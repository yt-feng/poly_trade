"""Offline maker-only contract for one continuous ten-fill cash ledger.

This module is deliberately separate from the frozen H30 taker contract.  It
checks the extra fields a future owner-scoped maker bundle would need:
post-only acceptance, the distinction between CLOB ``owner`` and on-chain
``makerAddress``, maker-owned ``OrderFilled`` events, zero taker fee, a
daily rebate batch allocated without double counting, and one chronological
reusable-cash chain.
The inputs are already captured artifacts.  Passing this function is only an
offline consistency result; it never authenticates a receipt or counts a live
Canary fill.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

try:  # Support both ``analysis.<module>`` tests and script-style imports.
    from .authenticated_compound_reconciler import reconcile_authenticated_chain
    from .orderfilled_compound_binding import audit_orderfilled_bindings
except ImportError:  # pragma: no cover - used by standalone repository tools.
    from authenticated_compound_reconciler import reconcile_authenticated_chain
    from orderfilled_compound_binding import audit_orderfilled_bindings


REQUIRED_FILLS = 10
ZERO = Decimal("0")
TOLERANCE = Decimal("0.000001")


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


def _same(left: Any, right: Any, field: str) -> bool:
    try:
        return abs(_decimal(left, field) - _decimal(right, field)) <= TOLERANCE
    except ValueError:
        return False


def _failure(reason: str, *, issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evidence_scope": "offline_maker_compound_consistency_only",
        "execution_role": "MAKER",
        "required_fills": REQUIRED_FILLS,
        "fills": 0,
        "same_ledger_compounding_rows": 0,
        "offline_consistency_passed": False,
        "paper_replay_passed": False,
        "receipt_authenticity_verified": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "gate_passed": False,
        "failure_reason": reason,
        "issues": issues or [],
    }


def validate_maker_compound_chain(
    events: Iterable[dict[str, Any]],
    orderfilled: Iterable[dict[str, Any]],
    *,
    owner: str,
    maker_address: Any = None,
    initial_cash: Any = "10",
    minimum_order: Any = "0.0000001",
    starting_checkpoint_id: Any = None,
    closing_balance_checkpoint_id: Any = None,
    closing_balance: Any = None,
    require_rebate: bool = True,
    require_async_binding: bool = False,
) -> dict[str, Any]:
    """Validate an independent maker-only ten-row compound bundle.

    ``events`` must carry ``payout`` equal to settlement payout plus the
    independently identified maker rebate.  The shared authenticated
    reconciler still supplies the no-reset balance chain and explicit opening
    and closing anchors.  This function does not make the supplied identities
    externally authentic.
    """
    rows = list(events)
    fills = list(orderfilled)
    expected_owner = _text(owner)
    expected_maker = _text(maker_address) or expected_owner
    if not expected_owner:
        return _failure("owner_required")
    if type(require_rebate) is not bool:
        return _failure("require_rebate_must_be_bool")
    if type(require_async_binding) is not bool:
        return _failure("require_async_binding_must_be_bool")
    if len(rows) != REQUIRED_FILLS:
        return _failure("exactly_ten_maker_events_required")
    if len(fills) != REQUIRED_FILLS:
        return _failure("exactly_ten_orderfilled_records_required")

    issues: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {
        "order_id": set(),
        "orderfilled_event_id": set(),
    }
    rebate_batches: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return _failure("maker_event_must_be_object")
        sequence = row.get("sequence_no")
        if type(sequence) is not int or not 1 <= sequence <= REQUIRED_FILLS:
            issues.append({"reason": "sequence_no_invalid", "sequence_no": sequence})
            continue
        if _text(row.get("execution_role")).upper() != "MAKER":
            issues.append({"reason": "execution_role_must_be_maker", "sequence_no": sequence})
        if _text(row.get("trader_side")).upper() != "MAKER":
            issues.append({"reason": "trade_side_must_be_maker", "sequence_no": sequence})
        if _text(row.get("trade_status")).upper() not in {"TRADE_STATUS_CONFIRMED", "CONFIRMED"}:
            issues.append({"reason": "trade_status_must_be_confirmed", "sequence_no": sequence})
        if _text(row.get("owner")) != expected_owner:
            issues.append({"reason": "owner_scope_mismatch", "sequence_no": sequence})
        if _text(row.get("maker_address")).lower() != expected_maker.lower():
            issues.append({"reason": "maker_address_scope_mismatch", "sequence_no": sequence})
        if row.get("post_only_accepted") is not True:
            issues.append({"reason": "post_only_acceptance_required", "sequence_no": sequence})
        if _text(row.get("order_status")).upper() not in {"MATCHED", "FILLED", "CONFIRMED"}:
            issues.append({"reason": "maker_order_not_filled", "sequence_no": sequence})
        if require_async_binding and row.get("async_trade_id_binding_ready") is not True:
            issues.append({"reason": "async_trade_id_binding_required", "sequence_no": sequence})
        try:
            fee_rate = _decimal(row.get("fee_rate"), "fee_rate")
            fee = _decimal(row.get("fee"), "fee")
            settlement_payout = _decimal(row.get("settlement_payout"), "settlement_payout")
            rebate_amount = _decimal(row.get("rebate_amount"), "rebate_amount")
            payout = _decimal(row.get("payout"), "payout")
        except ValueError as exc:
            issues.append({"reason": str(exc), "sequence_no": sequence})
            continue
        if fee_rate != ZERO:
            issues.append({"reason": "maker_fee_rate_must_be_zero", "sequence_no": sequence})
        if fee != ZERO:
            issues.append({"reason": "maker_fee_must_be_zero", "sequence_no": sequence})
        if settlement_payout < ZERO or rebate_amount < ZERO or payout < ZERO:
            issues.append({"reason": "maker_payout_amount_negative", "sequence_no": sequence})
        if not _same(payout, settlement_payout + rebate_amount, "payout"):
            issues.append({"reason": "settlement_plus_rebate_mismatch", "sequence_no": sequence})
        rebate_status = _text(row.get("rebate_status")).upper()
        if rebate_status == "CONFIRMED":
            batch_id = _text(row.get("rebate_batch_id"))
            receipt_id = _text(row.get("rebate_receipt_id"))
            if not batch_id:
                issues.append({"reason": "rebate_batch_id_required", "sequence_no": sequence})
            if not receipt_id:
                issues.append({"reason": "rebate_receipt_id_required", "sequence_no": sequence})
            try:
                batch_total = _decimal(row.get("rebate_batch_amount"), "rebate_batch_amount")
                allocated = _decimal(row.get("rebate_allocated_amount"), "rebate_allocated_amount")
                rebate_available_ts = int(str(row.get("rebate_available_ts")))
                cash_available_ts = int(str(row.get("cash_available_ts")))
            except (ValueError, TypeError, InvalidOperation):
                issues.append({"reason": "rebate_batch_amount_or_timestamp_invalid", "sequence_no": sequence})
            else:
                if batch_total < Decimal("1"):
                    issues.append({"reason": "rebate_batch_below_minimum_payout", "sequence_no": sequence})
                if not _same(allocated, rebate_amount, "rebate_allocated_amount"):
                    issues.append({"reason": "rebate_allocation_mismatch", "sequence_no": sequence})
                if rebate_available_ts > cash_available_ts:
                    issues.append({"reason": "rebate_available_after_cash_release", "sequence_no": sequence})
                if batch_id:
                    current = rebate_batches.setdefault(batch_id, {
                        "receipt_id": receipt_id,
                        "batch_amount": batch_total,
                        "allocated": ZERO,
                    })
                    if current["receipt_id"] != receipt_id or current["batch_amount"] != batch_total:
                        issues.append({"reason": "rebate_batch_identity_or_amount_mismatch", "sequence_no": sequence})
                    current["allocated"] += allocated
        elif rebate_status == "NOT_ELIGIBLE":
            if rebate_amount != ZERO:
                issues.append({"reason": "not_eligible_rebate_must_be_zero", "sequence_no": sequence})
            if require_rebate:
                issues.append({"reason": "maker_rebate_not_confirmed", "sequence_no": sequence})
        else:
            issues.append({"reason": "maker_rebate_status_invalid", "sequence_no": sequence})
        for field in seen:
            value = _text(row.get(field))
            if not value:
                issues.append({"reason": f"{field}_required", "sequence_no": sequence})
            elif value in seen[field]:
                issues.append({"reason": f"duplicate_{field}", "sequence_no": sequence})
            else:
                seen[field].add(value)

    sequences = [row.get("sequence_no") for row in rows if isinstance(row, dict)]
    if sequences != list(range(1, REQUIRED_FILLS + 1)):
        issues.append({"reason": "sequence_no_must_be_exactly_one_to_ten"})
    for batch_id, batch in rebate_batches.items():
        if abs(batch["allocated"] - batch["batch_amount"]) > TOLERANCE:
            issues.append({"reason": "rebate_batch_allocation_total_mismatch", "rebate_batch_id": batch_id})
    if require_rebate and not rebate_batches:
        issues.append({"reason": "confirmed_rebate_batch_required"})
    if issues:
        return _failure("maker_event_contract_failed", issues=issues)

    binding = audit_orderfilled_bindings(
        rows,
        fills,
        owner_address=expected_maker,
        execution_role="MAKER",
        expected_side="BUY",
    )
    if not binding["orderfilled_binding_ready"]:
        return _failure("maker_orderfilled_binding_failed", issues=binding["issues"])

    reconciled = reconcile_authenticated_chain(
        rows,
        owner=expected_owner,
        initial_cash=initial_cash,
        minimum_order=minimum_order,
        required_fills=REQUIRED_FILLS,
        starting_checkpoint_id=starting_checkpoint_id,
        closing_balance_checkpoint_id=closing_balance_checkpoint_id,
        closing_balance=closing_balance,
    )
    result = {
        **reconciled,
        "schema_version": 1,
        "evidence_scope": "offline_maker_compound_consistency_only",
        "execution_role": "MAKER",
        "maker_orderfilled_binding": binding,
        "receipt_authenticity_verified": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "offline_consistency_passed": bool(reconciled.get("gate_passed")),
        "same_ledger_compounding_rows": 10 if reconciled.get("gate_passed") else 0,
        "gate_passed": False,
    }
    if reconciled.get("gate_passed"):
        result["paper_replay_passed"] = True
        result["failure_reason"] = "offline_only_receipt_authentication_pending"
    return result


__all__ = ["validate_maker_compound_chain"]
