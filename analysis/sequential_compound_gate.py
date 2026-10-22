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
    require_prior_cash_release: bool = False,
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
    may overlap by default, but every entry is funded from the same cash ledger:
    settled payouts are released before each entry whose timestamp has arrived.
    When ``require_prior_cash_release`` is true, the chain is the stricter
    single-position timing interpretation: after the first entry, each next
    entry must occur strictly after the prior entry's cash-release timestamp.
    A settled loss is allowed to release zero cash; it may continue only when
    the same evolving ledger still has enough free cash for the next order.
    This preserves the user-defined continuous-compound Canary contract while
    preventing a reset-per-trial calculation from hiding an unfunded entry.
    The
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
    if type(require_cash_release_receipt) is not bool:
        raise ValueError("require_cash_release_receipt must be a bool")
    if type(require_prior_cash_release) is not bool:
        raise ValueError("require_prior_cash_release must be a bool")
    cash = _money(initial_cash, "initial_cash")
    start_cash = cash
    floor = _money(minimum_order, "minimum_order")
    if cash <= 0 or floor <= 0:
        raise ValueError("cash and minimum_order must be positive")

    rows: list[dict[str, Any]] = []
    failure_reason: str | None = None
    minimum_cash = cash
    minimum_cash_before_entry = cash
    pending: list[tuple[Decimal, Decimal]] = []
    previous_entry: Decimal | None = None
    previous_cash_available: Decimal | None = None
    stopped_after_required = False
    # Keep a redacted, sequence-level capital diagnostic alongside the normal
    # failure reason.  It deliberately contains no receipt, owner, market, or
    # wallet identifiers: the admission layer only needs to know where the
    # single evolving cash ledger first stopped funding the next entry.
    capital_failure_sequence: int | None = None
    capital_failure_cash: Decimal | None = None
    capital_failure_required_cash: Decimal | None = None
    # Paper rows normally do not carry account identities.  When a caller
    # does provide them, preserve the same-ledger contract instead of silently
    # accepting two reset ledgers concatenated into one ten-row sequence.
    ledger_id: str | None = None
    chain_id: str | None = None

    for index, event in enumerate(events, start=1):
        if stop_after_required_fills and len(rows) >= required_fills:
            stopped_after_required = True
            break
        if not isinstance(event, dict):
            failure_reason = "invalid_event"
            break
        supplied_ledger = str(event.get("ledger_id", "")).strip()
        supplied_chain = str(event.get("chain_id", "")).strip()
        if supplied_ledger or supplied_chain:
            if not supplied_ledger or not supplied_chain:
                failure_reason = "ledger_identity_incomplete"
                break
            if ledger_id is None:
                ledger_id = supplied_ledger
                chain_id = supplied_chain
            elif supplied_ledger != ledger_id:
                failure_reason = "ledger_id_mismatch"
                break
            elif supplied_chain != chain_id:
                failure_reason = "chain_id_mismatch"
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
        # A compound Canary is a chronological chain.  Equal timestamps do
        # not establish which fill funded the next entry, so they must fail
        # closed instead of being treated as an ordered pair.
        if previous_entry is not None and entry_ts <= previous_entry:
            failure_reason = "events_not_chronological"
            break
        if require_prior_cash_release and previous_entry is not None:
            if previous_cash_available is None or entry_ts <= previous_cash_available:
                failure_reason = "prior_cash_release_must_precede_next_entry"
                break
        previous_entry = entry_ts
        released_this_entry = Decimal("0")
        while pending and pending[0][0] <= entry_ts:
            _, payout_due = heapq.heappop(pending)
            cash += payout_due
            released_this_entry += payout_due
        minimum_cash_before_entry = min(minimum_cash_before_entry, cash)
        # The venue minimum is an account-level free-cash requirement, not
        # merely a condition on the amount eventually filled.  A partial
        # paper fill must not turn a balance below the minimum into a valid
        # next compound entry.
        if cash < floor:
            failure_reason = "insufficient_cash_for_minimum_order"
            capital_failure_sequence = index
            capital_failure_cash = cash
            capital_failure_required_cash = floor
            break
        reserved = cost + fee
        if reserved > cash:
            failure_reason = "insufficient_cash_for_next_entry"
            capital_failure_sequence = index
            capital_failure_cash = cash
            capital_failure_required_cash = reserved
            break
        cash_before = cash
        cash -= reserved
        minimum_cash = min(minimum_cash, cash)
        heapq.heappush(pending, (available_ts, payout))
        previous_cash_available = available_ts
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
    capital_floor_diagnostic = {
        "protocol": "h643_capital_buffer_loss_ladder_v1",
        "same_evolving_ledger": True,
        "minimum_entry_cash": str(floor),
        "first_unfunded_sequence": capital_failure_sequence,
        "cash_before_first_unfunded_entry": (
            str(capital_failure_cash) if capital_failure_cash is not None else None
        ),
        "required_cash_for_first_unfunded_entry": (
            str(capital_failure_required_cash)
            if capital_failure_required_cash is not None
            else None
        ),
        "minimum_cash_before_admitted_entry": str(minimum_cash_before_entry),
        "status": "blocked" if capital_failure_sequence is not None else "not_blocked",
    }
    return {
        "required_fills": int(required_fills),
        "fills": fills,
        "consecutive": fills == len(rows),
        "stopped_after_required_fills": stopped_after_required,
        "ending_cash": str(cash),
        "net_pnl": str(cash - start_cash),
        "minimum_cash": str(minimum_cash),
        "minimum_cash_before_entry": str(minimum_cash_before_entry),
        "evidence_mode": evidence_mode,
        "paper_replay_passed": paper_replay_passed,
        "gate_passed": canary_gate_passed,
        "failure_reason": failure_reason,
        "capital_floor_diagnostic": capital_floor_diagnostic,
        "ledger": rows,
    }
