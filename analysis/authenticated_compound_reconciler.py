"""Join read-only authenticated receipts into the ten-fill compound gate.

The reconciler consumes already captured account evidence.  It has no network,
wallet, signing, order, cancel, sell, redeem, or balance-reading capability.
Each row must join one owner-scoped confirmed trade to one settlement receipt,
one distinct cash-release/redemption receipt, and one independent balance
checkpoint before it can enter the shared cash ledger.  A paper row is never
upgraded into an authenticated fill here.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sequential_compound_gate import evaluate_compound_chain


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return parsed


def reconcile_authenticated_chain(
    events: Iterable[dict[str, Any]],
    *,
    owner: str,
    initial_cash: Any = "10",
    minimum_order: Any = "5",
    required_fills: int = 10,
) -> dict[str, Any]:
    """Validate identities and evaluate one no-reset authenticated cash chain.

    The caller supplies the economic amounts and timestamps after reconciling
    the captured trade, settlement, cash-release/redemption, and balance
    records.  Identity checks are deliberately stricter than the paper replay:
    owner, ledger, chain, sequence, checkpoint, transaction, trade, settlement,
    cash-release, and balance identifiers must each be present and unique.
    This compatibility helper is itself a ten-event boundary; callers that
    have not produced the v2 ledger fields fail closed instead of replaying
    independently reset rows on a fresh balance.
    """
    expected_owner = _text(owner)
    if not expected_owner:
        raise ValueError("owner is required")
    if type(required_fills) is not int or required_fills != 10:
        return {
            "gate_passed": False,
            "fills": 0,
            "failure_reason": "required_fills_fixed_at_ten",
            "owner_scoped": False,
            "identity_complete": False,
        }
    rows = [deepcopy(row) for row in events]
    if len(rows) != 10:
        return {
            "gate_passed": False,
            "fills": 0,
            "failure_reason": "exactly_ten_authenticated_events_required",
            "owner_scoped": False,
            "identity_complete": False,
        }
    ledger_id = _text(rows[0].get("ledger_id")) if rows and isinstance(rows[0], dict) else ""
    chain_id = _text(rows[0].get("chain_id")) if rows and isinstance(rows[0], dict) else ""
    if not ledger_id:
        return {"gate_passed": False, "fills": 0, "failure_reason": "missing_ledger_id", "owner_scoped": False, "identity_complete": False}
    if not chain_id:
        return {"gate_passed": False, "fills": 0, "failure_reason": "missing_chain_id", "owner_scoped": False, "identity_complete": False}
    seen: dict[str, set[str]] = {
        "trade_id": set(),
        "transaction_ref": set(),
        "settlement_receipt_id": set(),
        "cash_release_receipt_id": set(),
        "balance_checkpoint_id": set(),
    }
    for row in rows:
        if not isinstance(row, dict):
            return {
                "gate_passed": False,
                "fills": 0,
                "failure_reason": "invalid_authenticated_event",
                "owner_scoped": False,
                "identity_complete": False,
            }
        if _text(row.get("owner")) != expected_owner:
            return {
                "gate_passed": False,
                "fills": 0,
                "failure_reason": "owner_scope_mismatch",
                "owner_scoped": False,
                "identity_complete": False,
            }
        if _text(row.get("ledger_id")) != ledger_id:
            return {"gate_passed": False, "fills": 0, "failure_reason": "ledger_id_mismatch", "owner_scoped": True, "identity_complete": False}
        if _text(row.get("chain_id")) != chain_id:
            return {"gate_passed": False, "fills": 0, "failure_reason": "chain_id_mismatch", "owner_scoped": True, "identity_complete": False}
        for field in seen:
            value = _text(row.get(field))
            if not value:
                return {
                    "gate_passed": False,
                    "fills": 0,
                    "failure_reason": f"missing_{field}",
                    "owner_scoped": True,
                    "identity_complete": False,
                }
            if value in seen[field]:
                return {
                    "gate_passed": False,
                    "fills": 0,
                    "failure_reason": f"duplicate_{field}",
                    "owner_scoped": True,
                    "identity_complete": False,
                }
            seen[field].add(value)
        if _text(row.get("cash_release_receipt_id")) == _text(row.get("settlement_receipt_id")):
            return {
                "gate_passed": False,
                "fills": 0,
                "failure_reason": "cash_release_receipt_not_independent",
                "owner_scoped": True,
                "identity_complete": False,
            }

    sequence_values = [row.get("sequence_no") for row in rows]
    if sequence_values != list(range(1, 11)):
        return {"gate_passed": False, "fills": 0, "failure_reason": "sequence_no_must_be_exactly_one_to_ten", "owner_scoped": True, "identity_complete": False}
    previous_checkpoint = ""
    previous_after = Decimal("10")
    previous_entry: int | None = None
    for index, row in enumerate(rows):
        checkpoint = _text(row.get("balance_checkpoint_id"))
        prior_checkpoint = _text(row.get("previous_checkpoint_id"))
        if not checkpoint or not prior_checkpoint:
            return {"gate_passed": False, "fills": 0, "failure_reason": "checkpoint_link_required", "owner_scoped": True, "identity_complete": False}
        if index and prior_checkpoint != previous_checkpoint:
            return {"gate_passed": False, "fills": 0, "failure_reason": "previous_checkpoint_link_mismatch", "owner_scoped": True, "identity_complete": False}
        entry_raw = row.get("entry_ts")
        try:
            entry_ts = int(str(entry_raw))
            balance_before = _decimal(row.get("balance_before"), "balance_before")
            balance_after = _decimal(row.get("balance_after"), "balance_after")
            released = _decimal(row.get("released_cash_since_previous_entry"), "released_cash_since_previous_entry")
            cost = _decimal(row.get("entry_cost"), "entry_cost")
            fee = _decimal(row.get("fee"), "fee")
        except (ValueError, TypeError, InvalidOperation) as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) else "invalid_ledger_amount_or_timestamp"
            return {"gate_passed": False, "fills": 0, "failure_reason": reason, "owner_scoped": True, "identity_complete": False}
        if previous_entry is not None and entry_ts <= previous_entry:
            return {"gate_passed": False, "fills": 0, "failure_reason": "entry_timestamps_must_increase", "owner_scoped": True, "identity_complete": False}
        if balance_before != previous_after + released:
            return {"gate_passed": False, "fills": 0, "failure_reason": "balance_before_arithmetic_mismatch", "owner_scoped": True, "identity_complete": False}
        if balance_after != balance_before - cost - fee:
            return {"gate_passed": False, "fills": 0, "failure_reason": "balance_after_arithmetic_mismatch", "owner_scoped": True, "identity_complete": False}
        if balance_after < 0 or released < 0 or cost < 0 or fee < 0:
            return {"gate_passed": False, "fills": 0, "failure_reason": "negative_ledger_amount", "owner_scoped": True, "identity_complete": False}
        previous_checkpoint = checkpoint
        previous_after = balance_after
        previous_entry = entry_ts

    result = evaluate_compound_chain(
        rows,
        initial_cash=initial_cash,
        minimum_order=minimum_order,
        required_fills=required_fills,
        evidence_mode="authenticated",
        require_cash_release_receipt=True,
    )
    result.update({
        "owner_scoped": True,
        "identity_complete": True,
        "unique_trade_ids": len(seen["trade_id"]),
        "unique_transaction_refs": len(seen["transaction_ref"]),
        "unique_settlement_receipts": len(seen["settlement_receipt_id"]),
        "unique_cash_release_receipts": len(seen["cash_release_receipt_id"]),
        "unique_balance_checkpoints": len(seen["balance_checkpoint_id"]),
    })
    return result
