"""Fail-closed contract for a settlement-grounded whale-flow bundle.

H4 is a future protocol.  It is deliberately separate from quote replay and
from the maker lifecycle adapter: a public print, a book snapshot, or a
maker-side event cannot be relabelled as a taker whale signal.  This module
only validates an already captured bundle and never fetches a chain, account,
or market.  Even a consistent synthetic bundle is reported as offline-only
and never counted as a Canary fill.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

try:
    from .authenticated_compound_reconciler import reconcile_authenticated_chain
except ImportError:  # pragma: no cover
    from authenticated_compound_reconciler import reconcile_authenticated_chain


REQUIRED_FILLS = 10
ZERO = Decimal("0")

H4_SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "token_market_join": ("token_id", "condition_id", "market_slug", "outcome_index", "outcome_name"),
    "owner_scope": ("owner", "ledger_id", "chain_id"),
    "onchain_taker_fill": (
        "transaction_hash", "log_index", "block_number", "maker_address",
        "taker_address", "trader_side", "trader_side_source", "filled_shares", "fill_price", "fee",
    ),
    "fee_contract": ("fee_schedule_source", "fee_schedule_observed_ts"),
    "whale_observation": (
        "whale_address", "whale_trade_id", "whale_transaction_hash",
        "whale_observed_ts", "whale_observation_verified",
    ),
    "settlement": ("settlement_status", "settlement_receipt_id", "settlement_transaction_hash"),
    "cash_release": ("cash_release_status", "cash_release_receipt_id", "balance_checkpoint_id"),
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not out.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return out


def _failure(reason: str, issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema": "h4-settlement-grounded-whale-flow-v1",
        "evidence_scope": "offline_h4_bundle_consistency_only",
        "required_fills": REQUIRED_FILLS,
        "fills": 0,
        "same_ledger_compounding_rows": 0,
        "offline_consistency_passed": False,
        "receipt_authenticity_verified": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "gate_passed": False,
        "failure_reason": reason,
        "issues": issues or [],
    }


def audit_h4_source_schema(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Report whether captured records can even enter the H4 validator.

    This is a schema preflight, not a fill counter.  It accepts aliases used by
    normalized V2 OrderFilled records (``tx_hash``/``taker``/``size``), but it
    never treats those aliases as proof of owner scope or settlement.
    """
    rows = list(records)
    names: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            names.update(str(key) for key in row)
    aliases = {
        "transaction_hash": {"transaction_hash", "tx_hash"},
        "maker_address": {"maker_address", "maker"},
        "taker_address": {"taker_address", "taker"},
        "trader_side": {"trader_side"},
        "trader_side_source": {"trader_side_source"},
        "filled_shares": {"filled_shares", "size"},
        "fill_price": {"fill_price", "price"},
    }
    field_presence: dict[str, bool] = {}
    missing_by_group: dict[str, list[str]] = {}
    for group, fields in H4_SOURCE_GROUPS.items():
        missing: list[str] = []
        for field in fields:
            options = aliases.get(field, {field})
            present = bool(options & names)
            field_presence[field] = present
            if not present:
                missing.append(field)
        missing_by_group[group] = missing
    return {
        "schema": "h4-source-schema-audit-v1",
        "record_count": len(rows),
        "field_presence": field_presence,
        "missing_by_group": missing_by_group,
        "token_market_join_ready": not missing_by_group["token_market_join"],
        "owner_scoped_receipts_ready": not (
            missing_by_group["owner_scope"]
            or missing_by_group["settlement"]
            or missing_by_group["cash_release"]
        ),
        "h4_source_ready": not any(missing_by_group.values()),
        "strategy_evaluated": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }


def validate_h4_chain(
    events: Iterable[dict[str, Any]],
    *,
    owner: str,
    opening_checkpoint_id: Any,
    closing_checkpoint_id: Any,
    closing_balance: Any,
    initial_cash: Any = "10",
    minimum_order: Any = "5",
    target_cash: Any = "1000",
) -> dict[str, Any]:
    """Validate ten chronological taker rows with token/market/whale joins.

    The account owner must be the on-chain taker on every row.  Each row also
    carries a separately identified whale observation and a token-to-market
    join.  Settlement and cash-release receipts are independent fields, and
    the generic reconciler enforces the no-reset cash arithmetic.
    """
    expected_owner = _text(owner).lower()
    if not expected_owner:
        return _failure("owner_required")
    rows = list(events)
    if len(rows) != REQUIRED_FILLS:
        return _failure("exactly_ten_h4_events_required")
    try:
        target = _decimal(target_cash, "target_cash")
    except ValueError:
        return _failure("target_cash_must_be_finite_decimal")
    if target <= ZERO:
        return _failure("target_cash_must_be_positive")
    seen: dict[str, set[str]] = {key: set() for key in (
        "transaction_hash", "trade_id", "settlement_receipt_id",
        "cash_release_receipt_id", "balance_checkpoint_id", "whale_trade_id",
    )}
    issues: list[dict[str, Any]] = []
    identities: tuple[str, ...] = (
        "token_id", "condition_id", "market_slug", "outcome_name",
        "source_version", "identity_hash", "whale_address", "whale_trade_id",
        "whale_transaction_hash", "transaction_hash", "trade_id", "order_hash",
        "maker_address", "taker_address", "trader_side_source", "settlement_receipt_id",
        "settlement_transaction_hash", "settlement_status", "cash_release_receipt_id",
        "fee_schedule_source", "fee_schedule_observed_ts",
        "balance_checkpoint_id", "previous_checkpoint_id",
    )
    for row in rows:
        if not isinstance(row, dict):
            return _failure("h4_event_must_be_object")
        sequence = row.get("sequence_no")
        if type(sequence) is not int or not 1 <= sequence <= REQUIRED_FILLS:
            issues.append({"reason": "sequence_no_invalid", "sequence_no": sequence})
        for field in identities:
            if not _text(row.get(field)):
                issues.append({"reason": f"{field}_required", "sequence_no": sequence})
        for field in seen:
            value = _text(row.get(field))
            if value and value in seen[field]:
                issues.append({"reason": f"duplicate_{field}", "sequence_no": sequence})
            elif value:
                seen[field].add(value)
        if _text(row.get("owner")).lower() != expected_owner:
            issues.append({"reason": "owner_scope_mismatch", "sequence_no": sequence})
        if _text(row.get("taker_address")).lower() != expected_owner:
            issues.append({"reason": "owner_must_be_onchain_taker", "sequence_no": sequence})
        if _text(row.get("trader_side")).upper() != "TAKER":
            issues.append({"reason": "trader_side_must_be_taker", "sequence_no": sequence})
        if _text(row.get("trader_side_source")).upper() != "SETTLED_TAKER_LEG":
            issues.append({"reason": "trader_side_source_must_be_settled_taker_leg", "sequence_no": sequence})
        if _text(row.get("tx_status")).upper() not in {"CONFIRMED", "SUCCESS", "MINED"}:
            issues.append({"reason": "transaction_must_be_confirmed", "sequence_no": sequence})
        if row.get("market_join_verified") is not True:
            issues.append({"reason": "token_market_join_required", "sequence_no": sequence})
        if row.get("whale_observation_verified") is not True:
            issues.append({"reason": "whale_observation_must_be_verified", "sequence_no": sequence})
        if _text(row.get("settlement_status")).upper() != "CONFIRMED":
            issues.append({"reason": "settlement_must_be_confirmed", "sequence_no": sequence})
        if _text(row.get("cash_release_status")).upper() != "CONFIRMED":
            issues.append({"reason": "cash_release_must_be_confirmed", "sequence_no": sequence})
        if _text(row.get("whale_address")).lower() == expected_owner:
            issues.append({"reason": "whale_address_must_be_separate_from_account_owner", "sequence_no": sequence})
        try:
            block = int(str(row.get("block_number")))
            log_index = int(str(row.get("log_index")))
            outcome_index = int(str(row.get("outcome_index")))
            entry_ts = int(str(row.get("entry_ts")))
            whale_ts = int(str(row.get("whale_observed_ts")))
            fee_schedule_ts = int(str(row.get("fee_schedule_observed_ts")))
            if block < 0 or log_index < 0 or outcome_index < 0:
                raise ValueError
            if whale_ts > entry_ts:
                issues.append({"reason": "whale_observation_after_entry", "sequence_no": sequence})
            if fee_schedule_ts > entry_ts:
                issues.append({"reason": "fee_schedule_observation_after_entry", "sequence_no": sequence})
        except (TypeError, ValueError):
            issues.append({"reason": "onchain_identity_or_whale_clock_invalid", "sequence_no": sequence})
        try:
            shares = _decimal(row.get("filled_shares"), "filled_shares")
            price = _decimal(row.get("fill_price"), "fill_price")
            whale_notional = _decimal(row.get("whale_notional_usdc"), "whale_notional_usdc")
            if shares <= ZERO or not ZERO < price < Decimal("1") or whale_notional <= ZERO:
                raise ValueError
        except ValueError:
            issues.append({"reason": "fill_or_whale_amount_invalid", "sequence_no": sequence})
    if [row.get("sequence_no") for row in rows] != list(range(1, REQUIRED_FILLS + 1)):
        issues.append({"reason": "sequence_no_must_be_exactly_one_to_ten"})
    if issues:
        return _failure("h4_event_contract_failed", issues)

    mapped: list[dict[str, Any]] = []
    for row in rows:
        mapped.append({
            **row,
            "status": "confirmed",
            "transaction_ref": row["transaction_hash"],
            "cash_reconciled": True,
            "independent_checkpoint": True,
        })
    reconciled = reconcile_authenticated_chain(
        mapped,
        owner=owner,
        initial_cash=initial_cash,
        minimum_order=minimum_order,
        required_fills=REQUIRED_FILLS,
        starting_checkpoint_id=opening_checkpoint_id,
        closing_balance_checkpoint_id=closing_checkpoint_id,
        closing_balance=closing_balance,
    )
    out = {
        **reconciled,
        "schema": "h4-settlement-grounded-whale-flow-v1",
        "evidence_scope": "offline_h4_bundle_consistency_only",
        "h4_whale_flow_join_verified": True,
        "receipt_authenticity_verified": False,
        "offline_consistency_passed": bool(reconciled.get("gate_passed")),
        "same_ledger_compounding_rows": 10 if reconciled.get("gate_passed") else 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "gate_passed": False,
        "target_cash": str(target),
        "target_growth_passed": False,
    }
    if reconciled.get("gate_passed"):
        out["failure_reason"] = "offline_only_receipt_authentication_pending"
        try:
            ending = _decimal(reconciled.get("ending_cash"), "ending_cash")
        except ValueError:
            ending = ZERO
        out["growth_multiple"] = str(ending / _decimal(initial_cash, "initial_cash"))
        out["target_growth_passed"] = ending >= target
    return out


__all__ = ["H4_SOURCE_GROUPS", "audit_h4_source_schema", "validate_h4_chain"]
