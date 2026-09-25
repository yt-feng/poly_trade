"""Read-only staged reason codes for future canary execution diagnostics.

Pure classification only. This module does not import an exchange client, read an
account, place/cancel orders, redeem positions, or infer private fills from
public market data. Callers must supply observed/planned facts explicitly.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
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
        status = str(record.get("order_status") or "").upper()
        if record.get("order_terminal") is True:
            return result("fill", "ORDER_TERMINAL_UNFILLED", final=True)
        if status in {"REJECTED", "FAILED", "ERROR"}:
            return result("order", "ORDER_REJECTED", final=True, detail=status)
        return result("fill", "ORDER_ACCEPTED_NOT_CONFIRMED_FILLED", final=False, detail=status)
    return None


def classify(record: dict) -> dict:
    """Return exactly one current-stage code without collapsing distinct causes."""
    lifecycle = existing_episode(record)
    if lifecycle is not None:
        return lifecycle
    if record.get("signal_qualified") not in (True, False) or "signal_qualified" not in record:
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
        if bool(record.get("order_terminal")):
            return result("fill", "ORDER_TERMINAL_UNFILLED", final=True)
        return result("fill", "FILL_STATUS_UNKNOWN", final=False)

    if confirmed < requested:
        return result("fill", "PARTIAL_CONFIRMED_FILL", final=bool(record.get("order_terminal")))

    if not bool(record.get("entry_order_terminal")):
        return result("fill", "CONFIRMED_FILL_ORDER_NOT_TERMINAL", final=False)

    if bool(record.get("reconciliation_unknown")):
        return result("reconciliation", "RECONCILIATION_UNKNOWN", final=False)

    if bool(record.get("cash_reconciled")) and bool(record.get("position_flat")):
        return result("cash", "CASH_RECONCILED_ROUNDTRIP_COMPLETE", final=True)

    if bool(record.get("exit_trade_confirmed")) and not bool(record.get("cash_reconciled")):
        return result("cash", "EXIT_CONFIRMED_CASH_CHECKPOINT_PENDING", final=False)

    if bool(record.get("market_expired")):
        if not bool(record.get("official_resolution_observed")):
            return result("settlement", "EXPIRED_AWAITING_OFFICIAL_RESOLUTION", final=False)
        if bool(record.get("redeem_confirmed")) and not bool(record.get("cash_reconciled")):
            return result("cash", "REDEEM_CONFIRMED_CASH_CHECKPOINT_PENDING", final=False)
        if bool(record.get("redeem_requested")) and not bool(record.get("redeem_confirmed")):
            return result("settlement", "REDEEM_REQUEST_PENDING_CONFIRMATION", final=False)
        if bool(record.get("redeemable")) and not bool(record.get("redeem_requested")):
            return result("settlement", "REDEEMABLE_NOT_REQUESTED", final=False)
        return result("settlement", "RESOLUTION_OBSERVED_SETTLEMENT_STATUS_UNKNOWN", final=False)

    return result("position", "CONFIRMED_ENTRY_POSITION_OPEN", final=False)


def summarize(records: list[dict]) -> dict:
    classified = [classify(r) for r in records]
    return {
        "episodes": len(classified),
        "by_reason_code": dict(Counter(x["reason_code"] for x in classified)),
        "by_stage": dict(Counter(x["stage"] for x in classified)),
        "final_episodes": sum(bool(x["final_for_episode"]) for x in classified),
        "nonfinal_episodes": sum(not bool(x["final_for_episode"]) for x in classified),
        "scope": "read-only supplied-evidence classification; no account or order API",
    }
