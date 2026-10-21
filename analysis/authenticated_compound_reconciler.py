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

try:  # Support package imports from tests as well as script-style tools.
    from .sequential_compound_gate import evaluate_compound_chain
except ImportError:  # pragma: no cover - used by standalone repository tools.
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
    starting_checkpoint_id: Any = None,
    closing_balance_checkpoint_id: Any = None,
    closing_balance: Any = None,
) -> dict[str, Any]:
    """Validate identities and evaluate one no-reset authenticated cash chain.

    The caller supplies the economic amounts and timestamps after reconciling
    the captured trade, settlement, cash-release/redemption, and balance
    records.  Identity checks are deliberately stricter than the paper replay:
    owner, ledger, chain, sequence, checkpoint, transaction, trade, settlement,
    cash-release, and balance identifiers must each be present and unique.
    The explicit opening and closing checkpoint anchors are required even for
    this compatibility helper. Without them, a caller could supply ten
    internally consistent rows while leaving the account slice undefined.
    These anchors do not authenticate their origin; they make the required
    ledger interval explicit so a v2 caller can bind it to captured evidence.
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
    try:
        expected_initial_cash = _decimal(initial_cash, "initial_cash")
        if expected_initial_cash != Decimal("10"):
            return {
                "gate_passed": False,
                "fills": 0,
                "failure_reason": "initial_cash_must_equal_ten",
                "owner_scoped": False,
                "identity_complete": False,
            }
        opening_checkpoint = _text(starting_checkpoint_id)
        closing_checkpoint = _text(closing_balance_checkpoint_id)
    except (ValueError, TypeError, InvalidOperation) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) else "ledger_anchor_required"
        return {
            "gate_passed": False,
            "fills": 0,
            "failure_reason": reason,
            "owner_scoped": False,
            "identity_complete": False,
        }
    if not opening_checkpoint or not closing_checkpoint:
        return {
            "gate_passed": False,
            "fills": 0,
            "failure_reason": "ledger_anchors_required",
            "owner_scoped": False,
            "identity_complete": False,
        }
    try:
        expected_closing_balance = _decimal(closing_balance, "closing_balance")
    except (ValueError, TypeError, InvalidOperation) as exc:
        return {
            "gate_passed": False,
            "fills": 0,
            "failure_reason": str(exc) if isinstance(exc, ValueError) and str(exc) else "closing_balance_required",
            "owner_scoped": False,
            "identity_complete": False,
        }
    if opening_checkpoint == closing_checkpoint:
        return {
            "gate_passed": False,
            "fills": 0,
            "failure_reason": "opening_and_closing_checkpoints_must_differ",
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
    previous_checkpoint = opening_checkpoint
    previous_after = expected_initial_cash
    previous_entry: int | None = None
    # Keep the receipt-linked payout schedule alongside the account ledger.
    # The balance fields below are caller-supplied observations; without this
    # schedule check, a bundle could declare an arbitrary
    # ``released_cash_since_previous_entry`` amount and still pass by changing
    # the closing balance to match the generic replay.  A release is admissible
    # only when it is exactly the sum of earlier row payouts whose cash was
    # available by this entry, with each receipt consumed once.
    scheduled_releases: list[tuple[Decimal, Decimal, str]] = []
    released_receipt_ids: set[str] = set()
    for index, row in enumerate(rows):
        checkpoint = _text(row.get("balance_checkpoint_id"))
        prior_checkpoint = _text(row.get("previous_checkpoint_id"))
        if not checkpoint or not prior_checkpoint:
            return {"gate_passed": False, "fills": 0, "failure_reason": "checkpoint_link_required", "owner_scoped": True, "identity_complete": False}
        if prior_checkpoint != previous_checkpoint:
            return {"gate_passed": False, "fills": 0, "failure_reason": "previous_checkpoint_link_mismatch", "owner_scoped": True, "identity_complete": False}
        entry_raw = row.get("entry_ts")
        try:
            entry_ts = int(str(entry_raw))
            balance_before = _decimal(row.get("balance_before"), "balance_before")
            balance_after = _decimal(row.get("balance_after"), "balance_after")
            released = _decimal(row.get("released_cash_since_previous_entry"), "released_cash_since_previous_entry")
            cost = _decimal(row.get("entry_cost"), "entry_cost")
            fee = _decimal(row.get("fee"), "fee")
            payout = _decimal(row.get("payout"), "payout")
            available_ts = int(str(row.get("cash_available_ts")))
        except (ValueError, TypeError, InvalidOperation) as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) else "invalid_ledger_amount_or_timestamp"
            return {"gate_passed": False, "fills": 0, "failure_reason": reason, "owner_scoped": True, "identity_complete": False}
        if previous_entry is not None and entry_ts <= previous_entry:
            return {"gate_passed": False, "fills": 0, "failure_reason": "entry_timestamps_must_increase", "owner_scoped": True, "identity_complete": False}
        if available_ts <= entry_ts:
            # A release at the same timestamp as its own entry does not prove
            # that the cash was available before that entry.  Equal timestamps
            # are therefore fail-closed at the authenticated boundary.
            return {"gate_passed": False, "fills": 0, "failure_reason": "cash_available_timestamp_must_follow_entry", "owner_scoped": True, "identity_complete": False}
        if index == 0 and released != Decimal("0"):
            return {"gate_passed": False, "fills": 0, "failure_reason": "first_entry_cannot_use_prior_release", "owner_scoped": True, "identity_complete": False}
        if balance_before != previous_after + released:
            return {"gate_passed": False, "fills": 0, "failure_reason": "balance_before_arithmetic_mismatch", "owner_scoped": True, "identity_complete": False}
        if balance_after != balance_before - cost - fee:
            return {"gate_passed": False, "fills": 0, "failure_reason": "balance_after_arithmetic_mismatch", "owner_scoped": True, "identity_complete": False}
        if balance_after < 0 or released < 0 or cost < 0 or fee < 0 or payout < 0:
            return {"gate_passed": False, "fills": 0, "failure_reason": "negative_ledger_amount", "owner_scoped": True, "identity_complete": False}
        expected_released = Decimal("0")
        due_receipt_ids: set[str] = set()
        for due_ts, due_amount, due_receipt_id in scheduled_releases:
            if due_ts == entry_ts and due_receipt_id not in released_receipt_ids:
                return {
                    "gate_passed": False,
                    "fills": 0,
                    "failure_reason": "cash_available_timestamp_must_precede_next_entry",
                    "owner_scoped": True,
                    "identity_complete": False,
                }
            if due_ts < entry_ts and due_receipt_id not in released_receipt_ids:
                expected_released += due_amount
                due_receipt_ids.add(due_receipt_id)
        if released != expected_released:
            return {"gate_passed": False, "fills": 0, "failure_reason": "released_cash_amount_mismatch", "owner_scoped": True, "identity_complete": False}
        # A settled loss is a valid zero-payout release event.  The evolving
        # balance must still fund the next minimum order; the shared gate
        # rejects an unfunded entry and never resets the opening cash.
        released_receipt_ids.update(due_receipt_ids)
        scheduled_releases.append((Decimal(str(available_ts)), payout, _text(row.get("cash_release_receipt_id"))))
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
        require_prior_cash_release=True,
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
    if result.get("failure_reason"):
        return result
    if closing_checkpoint in seen["balance_checkpoint_id"]:
        return {
            **result,
            "gate_passed": False,
            "fills": 0,
            "failure_reason": "closing_checkpoint_must_be_independent",
            "identity_complete": False,
        }
    try:
        ending_cash = _decimal(result.get("ending_cash"), "ending_cash")
    except (ValueError, TypeError, InvalidOperation) as exc:
        return {
            **result,
            "gate_passed": False,
            "fills": 0,
            "failure_reason": str(exc) if isinstance(exc, ValueError) and str(exc) else "ending_cash_missing",
            "identity_complete": False,
        }
    if expected_closing_balance != ending_cash:
        return {
            **result,
            "gate_passed": False,
            "fills": 0,
            "failure_reason": "closing_balance_arithmetic_mismatch",
            "identity_complete": False,
        }
    return result
