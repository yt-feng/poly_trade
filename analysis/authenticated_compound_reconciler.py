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
from typing import Any, Iterable

from sequential_compound_gate import evaluate_compound_chain


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


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
    owner, transaction, trade, settlement, cash-release, and balance
    identifiers must each be present and unique.
    """
    expected_owner = _text(owner)
    if not expected_owner:
        raise ValueError("owner is required")
    rows = [deepcopy(row) for row in events]
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
