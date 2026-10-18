"""Join a validated maker pair to an explicit cash checkpoint.

The function accepts only caller-supplied receipts.  It never reads a wallet,
creates a merge/redeem action, or treats a pair trade as reusable cash until
the complete account cash-flow slice and zero-position checkpoint reconcile.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from cash_amount_audit import audit_cash_amount


def _dec(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def reconcile_pair_cash(
    pair: dict[str, Any],
    *,
    opening_checkpoint: dict[str, Any],
    cash_flow_evidence: Iterable[dict[str, Any]],
    closing_checkpoint: dict[str, Any],
    expected_asset: str,
    next_minimum_order: Any = "5",
) -> dict[str, Any]:
    """Return reusable-cash evidence only after an independent cash audit."""
    result: dict[str, Any] = {
        "cash_reusable": False,
        "amount_verified": False,
        "pair_status": pair.get("pair_status") if isinstance(pair, dict) else None,
        "cash_checkpoint_id": None,
        "available_cash_amount": None,
        "reason": None,
    }
    if not isinstance(pair, dict) or pair.get("pair_status") != "settlement_candidate":
        result["reason"] = "PAIR_SETTLEMENT_CANDIDATE_REQUIRED"
        return result
    pair_ids = {str(value) for value in pair.get("pair_trade_ids", []) if str(value)}
    if len(pair_ids) != 2:
        result["reason"] = "PAIR_TRADE_IDS_REQUIRED"
        return result
    try:
        minimum = _dec(next_minimum_order, "next_minimum_order")
        if minimum <= 0:
            raise ValueError("NEXT_MINIMUM_ORDER_INVALID")
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    flows = list(cash_flow_evidence)
    flow_ids = {str(flow.get("economic_id")) for flow in flows if isinstance(flow, dict)}
    if not pair_ids.issubset(flow_ids):
        result["reason"] = "PAIR_TRADES_MISSING_FROM_CASH_SLICE"
        return result
    required_flow_event_ids = [
        str(flow.get("event_id"))
        for flow in flows
        if isinstance(flow, dict) and str(flow.get("economic_id")) in pair_ids
    ]
    audit = audit_cash_amount(
        opening_checkpoint,
        flows,
        closing_checkpoint,
        required_flow_event_ids,
        expected_asset,
    )
    result.update({
        "amount_verified": bool(audit.get("amount_verified")),
        "audit_reason": audit.get("reason"),
        "evidence_hash": audit.get("evidence_hash"),
        "cash_checkpoint_id": closing_checkpoint.get("balance_checkpoint_id"),
        "available_cash_amount": audit.get("available_cash"),
    })
    if not audit.get("amount_verified"):
        result["reason"] = "CASH_AMOUNT_AUDIT_FAILED"
        return result
    try:
        available = _dec(audit.get("available_cash"), "available_cash")
        reserved = _dec(audit.get("reserved_cash"), "reserved_cash")
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    if reserved != 0:
        result["reason"] = "CASH_REMAINS_RESERVED"
        return result
    if available < minimum:
        result["reason"] = "CASH_BELOW_NEXT_MINIMUM_ORDER"
        return result
    result["cash_reusable"] = True
    result["reason"] = "PAIR_SETTLEMENT_AND_CASH_CHECKPOINT_RECONCILED"
    return result
