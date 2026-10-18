"""Validate one chronological, cash-recycling Canary chain.

This is a pure ledger check.  It never submits an order and never treats a
paper label as a confirmed fill.  A chain can pass only when every event is a
confirmed fill, each entry is funded by the same evolving cash balance, and
the ending balance has not fallen below the starting balance.  A bounded
chain can explicitly stop after its required fill count; this is the contract
for a finite Canary batch rather than an assertion about later trades.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
import heapq


def _money(value: Any, field: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not out.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return out


def evaluate_compound_chain(
    events: Iterable[dict[str, Any]],
    *,
    initial_cash: Any = "10",
    minimum_order: Any = "5",
    required_fills: int = 10,
    evidence_mode: str = "authenticated",
    require_cash_release_receipt: bool = False,
    stop_after_required_fills: bool = False,
) -> dict[str, Any]:
    """Evaluate a single no-reset sequence of settled account events.

    Each event must contain ``status=confirmed``, ``entry_ts``,
    ``cash_available_ts``, ``entry_cost``, ``fee`` and ``payout``.  In
    ``authenticated`` mode it must also carry an owner-scoped trade reference,
    a settlement receipt, an independent balance checkpoint, and an explicit
    ``cash_reconciled=true`` assertion.  Callers that require an explicit
    settlement/redemption-to-cash boundary can set
    ``require_cash_release_receipt=True``; this adds a distinct
    ``cash_release_receipt_id`` requirement.  A confirmed trade alone is not
    enough to establish that the cash was released for the next fill.  Events
    may overlap, but every entry is funded from the same cash ledger: settled
    payouts are released before each entry whose timestamp has arrived.  The
    returned ``fills`` count is therefore a consecutive, reusable-cash chain,
    rather than an aggregate count across independent trials.  When
    ``stop_after_required_fills`` is true, processing ends immediately after
    the required number of valid entries and later events are outside the
    bounded batch.  This keeps a ten-fill Canary result from being invalidated
    by trades that were never part of that batch.
    """
    if required_fills <= 0:
        raise ValueError("required_fills must be positive")
    if evidence_mode not in {"authenticated", "paper_upper_bound"}:
        raise ValueError("unsupported evidence_mode")
    if type(stop_after_required_fills) is not bool:
        raise ValueError("stop_after_required_fills must be a bool")
    cash = _money(initial_cash, "initial_cash")
    start_cash = cash
    floor = _money(minimum_order, "minimum_order")
    if cash <= 0 or floor <= 0:
        raise ValueError("cash and minimum_order must be positive")

    rows: list[dict[str, Any]] = []
    failure_reason: str | None = None
    minimum_cash = cash
    pending: list[tuple[Decimal, Decimal]] = []
    previous_entry: Decimal | None = None
    stopped_after_required = False

    for index, event in enumerate(events, start=1):
        if stop_after_required_fills and len(rows) >= required_fills:
            stopped_after_required = True
            break
        if not isinstance(event, dict):
            failure_reason = "invalid_event"
            break
        status = str(event.get("status", "")).lower()
        accepted_statuses = {"confirmed"} if evidence_mode == "authenticated" else {"confirmed", "paper"}
        if status not in accepted_statuses:
            failure_reason = "unconfirmed_fill"
            break
        if evidence_mode == "authenticated":
            required_receipt_fields = [
                "trade_id",
                "transaction_ref",
                "settlement_receipt_id",
                "balance_checkpoint_id",
            ]
            if require_cash_release_receipt:
                required_receipt_fields.append("cash_release_receipt_id")
            if any(not str(event.get(field, "")).strip() for field in required_receipt_fields):
                failure_reason = "missing_authoritative_receipt_or_checkpoint"
                break
            if require_cash_release_receipt and str(event["cash_release_receipt_id"]).strip() == str(event["settlement_receipt_id"]).strip():
                failure_reason = "cash_release_receipt_not_independent"
                break
            if event.get("cash_reconciled") is not True:
                failure_reason = "cash_checkpoint_not_reconciled"
                break
            if event.get("independent_checkpoint") is not True:
                failure_reason = "checkpoint_not_independent"
                break
        try:
            entry_ts = _money(event.get("entry_ts"), "entry_ts")
            available_ts = _money(event.get("cash_available_ts"), "cash_available_ts")
            cost = _money(event.get("entry_cost"), "entry_cost")
            fee = _money(event.get("fee"), "fee")
            payout = _money(event.get("payout"), "payout")
        except ValueError:
            failure_reason = "invalid_event_amount_or_time"
            break
        if cost < floor or fee < 0 or payout < 0 or available_ts < entry_ts:
            failure_reason = "invalid_event_constraints"
            break
        if previous_entry is not None and entry_ts < previous_entry:
            failure_reason = "events_not_chronological"
            break
        previous_entry = entry_ts
        while pending and pending[0][0] <= entry_ts:
            _, payout_due = heapq.heappop(pending)
            cash += payout_due
        reserved = cost + fee
        if reserved > cash:
            failure_reason = "insufficient_cash_for_next_entry"
            break
        cash_before = cash
        cash -= reserved
        minimum_cash = min(minimum_cash, cash)
        heapq.heappush(pending, (available_ts, payout))
        rows.append(
            {
                "index": index,
                "cash_before_entry": str(cash_before),
                "reserved": str(reserved),
                "payout": str(payout),
                "cash_after_entry": str(cash),
            }
        )
        if stop_after_required_fills and len(rows) >= required_fills:
            stopped_after_required = True
            break

    while pending:
        _, payout_due = heapq.heappop(pending)
        cash += payout_due
    fills = len(rows)
    if failure_reason is None and fills < required_fills:
        failure_reason = "fewer_than_required_consecutive_fills"
    paper_replay_passed = failure_reason is None and fills >= required_fills and cash >= start_cash
    if failure_reason is None and cash < start_cash:
        failure_reason = "compound_chain_not_growth_positive"
    canary_gate_passed = paper_replay_passed and evidence_mode == "authenticated"
    return {
        "required_fills": int(required_fills),
        "fills": fills,
        "consecutive": fills == len(rows),
        "stopped_after_required_fills": stopped_after_required,
        "ending_cash": str(cash),
        "net_pnl": str(cash - start_cash),
        "minimum_cash": str(minimum_cash),
        "evidence_mode": evidence_mode,
        "paper_replay_passed": paper_replay_passed,
        "gate_passed": canary_gate_passed,
        "failure_reason": failure_reason,
        "ledger": rows,
    }
