"""Validate one chronological, cash-recycling Canary chain.

This is a pure ledger check.  It never submits an order and never treats a
paper label as a confirmed fill.  A chain can pass only when every event is a
confirmed fill, each entry is funded by the prior settled cash balance, and
the ending balance has not fallen below the starting balance.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


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
) -> dict[str, Any]:
    """Evaluate a single no-reset sequence of settled account events.

    Each event must contain ``status=confirmed``, ``entry_ts``,
    ``cash_available_ts``, ``entry_cost``, ``fee`` and ``payout``.  The next
    entry must occur after the previous event's cash is available.  The
    returned ``fills`` count is therefore a consecutive, reusable-cash chain,
    rather than an aggregate count across independent trials.
    """
    if required_fills <= 0:
        raise ValueError("required_fills must be positive")
    cash = _money(initial_cash, "initial_cash")
    start_cash = cash
    floor = _money(minimum_order, "minimum_order")
    if cash <= 0 or floor <= 0:
        raise ValueError("cash and minimum_order must be positive")

    rows: list[dict[str, Any]] = []
    previous_available: Decimal | None = None
    failure_reason: str | None = None
    minimum_cash = cash

    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            failure_reason = "invalid_event"
            break
        if str(event.get("status", "")).lower() != "confirmed":
            failure_reason = "unconfirmed_fill"
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
        if previous_available is not None and entry_ts < previous_available:
            failure_reason = "cash_not_reusable_before_next_entry"
            break
        reserved = cost + fee
        if reserved > cash:
            failure_reason = "insufficient_cash_for_next_entry"
            break
        cash_before = cash
        cash -= reserved
        cash += payout
        minimum_cash = min(minimum_cash, cash)
        previous_available = available_ts
        rows.append(
            {
                "index": index,
                "cash_before_entry": str(cash_before),
                "reserved": str(reserved),
                "payout": str(payout),
                "cash_after_settlement": str(cash),
            }
        )

    fills = len(rows)
    if failure_reason is None and fills < required_fills:
        failure_reason = "fewer_than_required_consecutive_fills"
    if failure_reason is None and cash < start_cash:
        failure_reason = "compound_chain_not_growth_positive"
    return {
        "required_fills": int(required_fills),
        "fills": fills,
        "consecutive": fills == len(rows),
        "ending_cash": str(cash),
        "net_pnl": str(cash - start_cash),
        "minimum_cash": str(minimum_cash),
        "gate_passed": failure_reason is None,
        "failure_reason": failure_reason,
        "ledger": rows,
    }
