"""Read-only staged reason codes for future canary execution diagnostics.

Pure classification only. This module does not import an exchange client, read an
account, place/cancel orders, redeem positions, or infer private fills from
public market data. Callers must supply observed/planned facts explicitly.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from typing import Any


def dec(value: Any, *, default: str | None = None) -> Decimal | None:
    if value in (None, ""):
        if default is None:
            return None
        value = default
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return out


def result(stage: str, code: str, *, final: bool, detail: str = "") -> dict:
    return {
        "stage": stage,
        "reason_code": code,
        "final_for_episode": bool(final),
        "detail": detail,
        "live_action_taken": False,
        "private_fill_inferred": False,
    }


def terminal_without_confirmed_fill(record: dict) -> dict:
    """Classify a terminal *order*, never assume that its trades are settled.

    ``fill_reconciliation_complete`` is an explicit caller assertion that all
    fills for this order are covered, including late events. It is not a receipt
    authenticator or permission to release cash. Existing amount/source audits
    remain required. Missing quantity is different from an observed zero.
    """
    confirmed = dec(record.get("confirmed_filled_shares"))
    if confirmed is None:
        return result("fill", "ORDER_TERMINAL_FILL_QUANTITY_UNKNOWN", final=False)
    if confirmed != 0:
        raise ValueError("terminal zero-fill classification requires an explicit zero")
    statuses = {str(record.get(k) or "").strip().upper()
                for k in ("order_status", "trade_status")}
    pending = {"MATCHED", "MINED", "RETRYING", "PENDING", "DELAYED"}
    matched = dec(record.get("matched_shares"))
    if matched is not None and matched < 0:
        raise ValueError("negative matched quantity")
    if statuses & pending or "CONFIRMED" in statuses or (matched is not None and matched > 0):
        return result("fill", "ORDER_TERMINAL_TRADE_EVIDENCE_PENDING", final=False,
                      detail="Order termination cannot resolve pending or contradictory trade evidence")
    if record.get("fill_reconciliation_complete") is not True:
        return result("fill", "ORDER_TERMINAL_FILL_RECONCILIATION_PENDING", final=False)
    return result("fill", "ORDER_TERMINAL_UNFILLED", final=True,
                  detail="Explicit zero and caller-declared complete fill coverage; no cash/source authentication")


def existing_episode(record: dict) -> dict | None:
    """Classify occupied or uncertain capital before rechecking entry signals.

    All values remain caller-supplied evidence. This function neither verifies
    private receipts nor makes cash reusable. A terminal order is not a closed
    position. Explicit submission evidence is required to prioritize a pending
    order over a new signal; a status string alone may belong to a plan fixture.
    """
    if record.get("reconciliation_unknown") is True:
        return result("reconciliation", "RECONCILIATION_UNKNOWN", final=False)
    confirmed = dec(record.get("confirmed_filled_shares"), default="0")
    requested = dec(record.get("requested_shares"))
    remaining = dec(record.get("remaining_position_shares"), default="0")
    if confirmed < 0 or remaining < 0:
        raise ValueError("negative confirmed or remaining quantity")
    if requested is not None and (requested <= 0 or confirmed > requested):
        raise ValueError("confirmed fill quantities are inconsistent")
    post_flags = any(record.get(k) is True for k in (
        "exit_trade_confirmed", "redeem_confirmed", "redeem_requested",
        "redeemable", "cash_reconciled"))
    if confirmed == 0 and (remaining > 0 or post_flags):
        return result("reconciliation", "POSITION_OR_CASH_EVIDENCE_INCOMPLETE", final=False)
    if confirmed > 0:
        terminal = record.get("entry_order_terminal") is True or record.get("order_terminal") is True
        flat = record.get("position_flat") is True
        if flat and remaining > 0:
            return result("reconciliation", "POSITION_EVIDENCE_CONFLICT", final=False)
        if record.get("cash_reconciled") is True and flat and terminal:
            return result("cash", "CASH_RECONCILED_ROUNDTRIP_COMPLETE", final=True,
                          detail="Classification only; cash/receipt authenticity is not verified here")
        if record.get("market_expired") is True:
            if record.get("official_resolution_observed") is not True:
                return result("settlement", "EXPIRED_AWAITING_OFFICIAL_RESOLUTION", final=False)
            if record.get("redeem_confirmed") is True:
                return result("cash", "REDEEM_CONFIRMED_CASH_CHECKPOINT_PENDING", final=False)
            if record.get("redeem_requested") is True:
                return result("settlement", "REDEEM_REQUEST_PENDING_CONFIRMATION", final=False)
            if record.get("redeemable") is True:
                return result("settlement", "REDEEMABLE_NOT_REQUESTED", final=False)
            return result("settlement", "RESOLUTION_OBSERVED_SETTLEMENT_STATUS_UNKNOWN", final=False)
        if record.get("exit_trade_confirmed") is True and remaining == 0:
            return result("cash", "EXIT_CONFIRMED_CASH_CHECKPOINT_PENDING", final=False)
        if requested is None:
            return result("fill", "CONFIRMED_FILL_REQUEST_SIZE_UNKNOWN", final=False)
        if confirmed < requested:
            return result("fill", "PARTIAL_CONFIRMED_FILL", final=False,
                          detail="Unfilled remainder may be terminal; confirmed inventory still needs resolution")
        if not terminal:
            return result("fill", "CONFIRMED_FILL_ORDER_NOT_TERMINAL", final=False)
        return result("position", "CONFIRMED_ENTRY_POSITION_OPEN", final=False)
    submitted = record.get("order_submitted") is True or bool(record.get("submission_reference"))
    if submitted:
        status = str(record.get("order_status") or "").strip().upper()
        terminal = record.get("order_terminal") is True or record.get("entry_order_terminal") is True
        if terminal:
            return terminal_without_confirmed_fill(record)
        if status in {"REJECTED", "FAILED", "ERROR"}:
            zero_fill = terminal_without_confirmed_fill(record)
            return result("order", "ORDER_REJECTED", final=zero_fill["final_for_episode"],
                          detail=status + "; " + zero_fill["reason_code"])
        return result("fill", "ORDER_ACCEPTED_NOT_CONFIRMED_FILLED", final=False, detail=status)
    return None


def classify(record: dict) -> dict:
    """Return exactly one current-stage code without collapsing distinct causes."""
    lifecycle = existing_episode(record)
    if lifecycle is not None:
        return lifecycle
    if type(record.get("signal_qualified")) is not bool:
        return result("signal", "SIGNAL_QUALIFICATION_UNKNOWN", final=False)
    if record.get("signal_qualified") is False:
        return result("signal", "NO_QUALIFIED_OPPORTUNITY", final=True)

    planned_shares = dec(record.get("planned_shares"))
    minimum = dec(record.get("min_order_size"))
    if planned_shares is None or minimum is None or minimum <= 0:
        return result("planning", "RULES_OR_SIZE_UNKNOWN", final=True)
    if planned_shares < minimum:
        return result("planning", "ALGO_SIZE_BELOW_EXCHANGE_MINIMUM", final=True)

    all_in = dec(record.get("planned_all_in_usd"))
    available = dec(record.get("available_cash_usd"))
    total = dec(record.get("total_cash_usd"))
    reserved = dec(record.get("reserved_cash_usd"), default="0")
    if all_in is not None and total is not None and all_in > total:
        return result("cash", "INSUFFICIENT_TOTAL_CASH", final=True)
    if all_in is not None and available is not None and all_in > available:
        if reserved is not None and reserved > 0:
            return result("cash", "AVAILABLE_CASH_RESERVED_OR_FROZEN", final=True)
        return result("cash", "INSUFFICIENT_AVAILABLE_CASH", final=True)

    submit_ms = dec(record.get("decision_to_submit_ms"))
    submit_limit = dec(record.get("max_decision_to_submit_ms"))
    if submit_ms is not None and submit_limit is not None and submit_ms > submit_limit:
        return result("latency", "COMPUTE_OR_SEND_DELAY_EXCEEDED", final=True)

    quote_age = dec(record.get("quote_age_ms"))
    quote_limit = dec(record.get("max_quote_age_ms"))
    if quote_age is not None and quote_limit is not None:
        if quote_age < 0 or quote_age > quote_limit:
            return result("quote", "QUOTE_STALE_OR_CLOCK_INVALID", final=True)

    current_ask = dec(record.get("current_executable_ask"))
    ceiling = dec(record.get("entry_price_ceiling"))
    if current_ask is not None and ceiling is not None and current_ask > ceiling:
        return result("quote", "LIMIT_PRICE_RAN_AWAY", final=True)

    depth = dec(record.get("displayed_ask_depth_shares"))
    if depth is not None and depth < planned_shares:
        return result("depth", "DISPLAYED_DEPTH_INSUFFICIENT", final=True)

    order_status = str(record.get("order_status") or "").upper()
    if order_status in {"REJECTED", "FAILED", "ERROR"}:
        return result("order", "ORDER_REJECTED", final=True, detail=order_status)

    confirmed = dec(record.get("confirmed_filled_shares"), default="0")
    requested = dec(record.get("requested_shares"), default=str(planned_shares))
    if confirmed is None or requested is None:
        raise ValueError("fill quantities are invalid")
    if confirmed < 0 or requested <= 0 or confirmed > requested:
        raise ValueError("confirmed fill quantities are inconsistent")

    if confirmed == 0:
        if order_status in {"ACCEPTED", "LIVE", "DELAYED", "MATCHED", "MINED", "PENDING"}:
            return result("fill", "ORDER_ACCEPTED_NOT_CONFIRMED_FILLED", final=False, detail=order_status)
        if record.get("order_terminal") is True or record.get("entry_order_terminal") is True:
            return terminal_without_confirmed_fill(record)
        return result("fill", "FILL_STATUS_UNKNOWN", final=False)



def audit_episode_identity(records: list[dict], *, as_of_ms: int | None = None) -> dict:
    """Count supplied cumulative snapshots without counting model views as fills.

    Optional strict contract: every row has execution_identity containing stable
    account_scope (network/account), collateral_asset, condition_id, episode_id,
    token_id; observation_ms is when this *whole state* was recorded. These are
    cumulative snapshots, not trade deltas. A caller must construct/verify that
    state before calling. A future row is rejected, not silently filtered.

    Strategy/model labels are annotations, never execution identity. Latest
    equal-time states must agree after removing only the documented annotations.
    No private receipt is authenticated, no cash is summed/released, and no
    strategy is selected. Even a consistent supplied count is not live proof.
    """
    out = {
        'schema_version': 1, 'status': 'NOT_EVALUABLE', 'issues': [],
        'record_count': len(records), 'unique_episode_count': None,
        'supplied_confirmed_entry_episodes': None,
        'supplied_cash_closed_episodes': None, 'nonfinal_episode_count': None,
        'redundant_or_historical_records': None, 'by_latest_reason_code': {},
        'cash_or_pnl_aggregated': False, 'private_receipts_authenticated': False,
        'canary_eligibility_evaluated': False, 'live_action_taken': False,
    }
    if type(as_of_ms) is not int or as_of_ms < 0:
        out['issues'] = ['EXPLICIT_AS_OF_REQUIRED']
        return out
    fields = ('account_scope', 'collateral_asset', 'condition_id', 'episode_id')
    def identity_text(value):
        return isinstance(value, str) and 0 < len(value) <= 512 and value.strip() == value
    groups, order_owners = {}, {}
    for row in records:
        ident = row.get('execution_identity')
        if not isinstance(ident, dict) or not all(identity_text(ident.get(k)) for k in fields + ('token_id',)):
            out['issues'] = ['COMPLETE_EXECUTION_IDENTITY_REQUIRED']
            return out
        at = row.get('observation_ms')
        if type(at) is not int or at < 0:
            out['issues'] = ['EXPLICIT_OBSERVATION_TIME_REQUIRED']
            return out
        if at > as_of_ms:
            out['issues'] = ['FUTURE_OBSERVATION_NOT_USABLE']
            return out
        key = tuple(ident[k] for k in fields)
        groups.setdefault(key, []).append(row)
        ref = row.get('submission_reference')
        if ref is not None:
            if not identity_text(ref):
                out['issues'] = ['INVALID_SUBMISSION_IDENTITY']
                return out
            order_key = (key[0], key[1], ref)
            if order_key in order_owners and order_owners[order_key] != key:
                out['issues'] = ['SUBMISSION_ALIASED_ACROSS_EPISODES']
                return out
            order_owners[order_key] = key
    annotations = frozenset(('strategy_id', 'strategy_ids', 'model_id', 'model_score'))
    latest_records, latest_classes = [], []
    for history in groups.values():
        if len({r['execution_identity']['token_id'] for r in history}) != 1:
            out['issues'] = ['TOKEN_IDENTITY_CONFLICT_WITHIN_EPISODE']
            return out
        last_time = max(r['observation_ms'] for r in history)
        latest = [r for r in history if r['observation_ms'] == last_time]
        try:
            # Unknown extra fields are retained, so differing economic facts
            # cannot be discarded merely because they are not used by classify.
            states = {json.dumps({k: v for k, v in r.items() if k not in annotations},
                       sort_keys=True, separators=(',', ':'), allow_nan=False) for r in latest}
        except (ValueError, TypeError):
            out['issues'] = ['SNAPSHOT_NOT_FINITE_JSON']
            return out
        if len(states) != 1:
            out['issues'] = ['LATEST_SNAPSHOT_CONFLICT']
            return out
        current = latest[0]
        amount = dec(current.get('confirmed_filled_shares'))
        if amount is None:
            out['issues'] = ['LATEST_CONFIRMED_QUANTITY_UNKNOWN']
            return out
        prior = [dec(r.get('confirmed_filled_shares')) for r in history]
        if any(v is not None and v > amount for v in prior):
            out['issues'] = ['CUMULATIVE_CONFIRMED_QUANTITY_REGRESSED']
            return out
        if amount > 0 and not identity_text(current.get('submission_reference')):
            out['issues'] = ['CONFIRMED_ENTRY_ORDER_IDENTITY_MISSING']
            return out
        latest_records.append(current)
        latest_classes.append(classify(current))
    out.update(status='CONSISTENT_SUPPLIED_SNAPSHOTS_NOT_AUTHENTICATED',
        unique_episode_count=len(groups),
        supplied_confirmed_entry_episodes=sum(dec(r['confirmed_filled_shares']) > 0 for r in latest_records),
        supplied_cash_closed_episodes=sum(r['reason_code'] == 'CASH_RECONCILED_ROUNDTRIP_COMPLETE' for r in latest_classes),
        nonfinal_episode_count=sum(not r['final_for_episode'] for r in latest_classes),
        redundant_or_historical_records=len(records)-len(groups),
        by_latest_reason_code=dict(Counter(r['reason_code'] for r in latest_classes)))
    return out


def summarize(records: list[dict], *, as_of_ms: int | None = None) -> dict:
    """Preserve legacy row counters; expose separately identity-audited counts.

    The historic `episodes` field assumes one row per episode and is retained for
    compatibility ONLY. It is not a confirmed-trade count. Consumers combining
    model views or state history must use episode_audit and check its status.
    Missing identity never silently becomes a strict count or a cash release.
    """
    classified = [classify(r) for r in records]
    return {
        'record_count': len(classified), 'legacy_counts_are_row_based': True,
        "episodes": len(classified),
        "by_reason_code": dict(Counter(x["reason_code"] for x in classified)),
        "by_stage": dict(Counter(x["stage"] for x in classified)),
        "final_episodes": sum(bool(x["final_for_episode"]) for x in classified),
        "nonfinal_episodes": sum(not bool(x["final_for_episode"]) for x in classified),
        'episode_audit': audit_episode_identity(records, as_of_ms=as_of_ms),
        "scope": "read-only supplied-evidence classification; no account or order API",
    }
