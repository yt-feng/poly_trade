"""Offline receipt/cash state contract for a future human-approved micro canary.

This module has no exchange client, wallet, signing, order submission, cancel,
sell, redeem, or balance-read capability. It only consumes externally supplied
receipts/observations and makes the accounting state explicit.

Trade fills fail closed unless the supplied evidence says CONFIRMED and includes
both a trade id and transaction reference. Quote-triggered 0.98 exit intent
requires a fresh executable best bid, current tick/minimum, and enough displayed
bid depth to exit the whole currently available position. Cash is not reusable
until a caller-supplied balance/position checkpoint covers every cash-affecting
event in the episode.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any


MAX_QUOTE_AGE_MS = Decimal("1500")


def dec(value: Any) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return out


def new_episode(
    *,
    local_id: str,
    token_id: str,
    min_sell_size: Any,
    exit_trigger: Any = "0.98",
    expected_cash_asset: str = "",
) -> dict:
    minimum = dec(min_sell_size)
    trigger = dec(exit_trigger)
    if not local_id or not token_id or minimum <= 0 or not Decimal("0") < trigger < Decimal("1"):
        raise ValueError("invalid episode identity or constraints")
    return {
        "schema": 2,
        "mode": "receipt_contract_no_order_capability",
        "local_id": str(local_id),
        "token_id": str(token_id),
        "state": "planned",
        "min_sell_size": str(minimum),
        "exit_trigger_best_bid": str(trigger),
        "expected_cash_asset": str(expected_cash_asset or ""),
        "entry_order_id": None,
        "entry_order_terminal": False,
        "entry_trade_ids": [],
        "entry_gross_filled_shares": "0",
        "position_net_shares": "0",
        "open_sell_orders": {},
        "exit_trade_ids": [],
        "reported_exit_proceeds_usd": "0",
        "settlement_receipt_id": None,
        "redeem_receipt_id": None,
        "cash_affecting_event_ids": [],
        "cash_checkpoint_covered_event_ids": [],
        "cash_checkpoint_id": None,
        "cash_reconciled": False,
        "reconciled_available_cash_amount": None,
        "reconciled_cash_asset": None,
        "cash_reusable": False,
        "completed_roundtrip": False,
        "halt_new_actions": False,
        "halt_reason": None,
        "exit_intent_ready": False,
        "last_best_bid": None,
        "last_quote_received_ms": None,
        "last_quote_block_reason": None,
        "last_tick_size": None,
        "last_bid_size": None,
        "seen_event_ids": [],
        "orders_submitted_by_contract": False,
    }


def reserved_sell_shares(state: dict) -> Decimal:
    return sum(
        (dec(v["remaining_shares"]) for v in state.get("open_sell_orders", {}).values()),
        Decimal("0"),
    )


def available_sell_shares(state: dict) -> Decimal:
    return max(
        Decimal("0"),
        dec(state.get("position_net_shares", "0")) - reserved_sell_shares(state),
    )


def _record_cash_event(state: dict, event_id: str) -> None:
    if event_id not in state["cash_affecting_event_ids"]:
        state["cash_affecting_event_ids"].append(event_id)


def _confirmed_trade(event: dict, label: str) -> tuple[str, str]:
    trade_id = str(event.get("trade_id") or "")
    transaction_ref = str(event.get("transaction_ref") or "")
    status = str(event.get("trade_status") or "").upper()
    if status != "CONFIRMED":
        raise ValueError(f"{label} requires terminal CONFIRMED trade status")
    if not trade_id or not transaction_ref:
        raise ValueError(f"{label} requires trade_id and transaction_ref")
    return trade_id, transaction_ref


def _confirmed_chain_receipt(event: dict, label: str) -> tuple[str, str]:
    receipt_id = str(event.get("receipt_id") or "")
    transaction_ref = str(event.get("transaction_ref") or "")
    status = str(event.get("status") or "").upper()
    if status not in {"STATE_CONFIRMED", "CONFIRMED"}:
        raise ValueError(f"{label} requires confirmed chain status")
    if not receipt_id or not transaction_ref:
        raise ValueError(f"{label} requires receipt_id and transaction_ref")
    return receipt_id, transaction_ref


def _halt_if_stranded_position(state: dict) -> None:
    position = dec(state["position_net_shares"])
    if (
        position > 0
        and reserved_sell_shares(state) == 0
        and position < dec(state["min_sell_size"])
    ):
        state["halt_new_actions"] = True
        state["halt_reason"] = "remaining_position_below_current_sell_minimum"
        state["exit_intent_ready"] = False
        state["state"] = "position_below_sell_minimum"


def _recompute_completion(state: dict) -> None:
    has_entry = bool(state["entry_trade_ids"])
    has_exit_or_settlement = bool(
        state["exit_trade_ids"]
        or state["settlement_receipt_id"]
        or state["redeem_receipt_id"]
    )
    flat = (
        dec(state["position_net_shares"]) == 0
        and reserved_sell_shares(state) == 0
    )
    done = (
        has_entry
        and bool(state["entry_order_terminal"])
        and has_exit_or_settlement
        and flat
        and bool(state["cash_reconciled"])
    )
    state["completed_roundtrip"] = done
    state["cash_reusable"] = done and not state["halt_new_actions"]
    if done:
        state["state"] = "cash_reconciled"


def _event_id(event: dict) -> str:
    value = str(event.get("event_id") or "")
    if not value:
        raise ValueError("event_id is required for idempotency")
    return value


def _quote_block_reason(state: dict, event: dict) -> tuple[str | None, Decimal, Decimal, Decimal]:
    bid = dec(event.get("best_bid"))
    bid_size = dec(event.get("bid_size"))
    minimum = dec(event.get("min_order_size"))
    tick = dec(event.get("tick_size"))
    now_ms = dec(event.get("now_ms"))
    received_ms = dec(event.get("quote_received_ms"))

    if not Decimal("0") <= bid <= Decimal("1"):
        raise ValueError("invalid best bid")
    if bid_size < 0 or minimum <= 0 or tick <= 0:
        raise ValueError("invalid quote size/minimum/tick")
    age = now_ms - received_ms
    if age < 0 or age > MAX_QUOTE_AGE_MS:
        return "quote_stale_or_future", bid, bid_size, minimum
    if bid % tick != 0:
        return "best_bid_off_current_tick", bid, bid_size, minimum
    if state["halt_new_actions"]:
        return "episode_halted", bid, bid_size, minimum
    if not state["entry_order_terminal"]:
        return "entry_order_not_terminal", bid, bid_size, minimum
    if not state["entry_trade_ids"]:
        return "no_confirmed_entry_trade", bid, bid_size, minimum
    if bid < dec(state["exit_trigger_best_bid"]):
        return "best_bid_below_098_trigger", bid, bid_size, minimum

    available = available_sell_shares(state)
    if available < minimum:
        return "available_position_below_current_sell_minimum", bid, bid_size, minimum
    if bid_size < available:
        return "best_bid_depth_insufficient_for_full_exit", bid, bid_size, minimum
    return None, bid, bid_size, minimum


def apply_event(original: dict, event: dict) -> dict:
    """Apply one observed receipt/market event; never creates an external action."""
    state = deepcopy(original)
    event_id = _event_id(event)
    if event_id in state["seen_event_ids"]:
        return state
    kind = str(event.get("kind") or "")
    if not kind:
        raise ValueError("event kind is required")
    state["seen_event_ids"].append(event_id)

    if kind == "entry_order_accepted":
        order_id = str(event.get("order_id") or "")
        if not order_id:
            raise ValueError("accepted entry requires order_id")
        state["entry_order_id"] = order_id
        state["state"] = "entry_accepted"

    elif kind == "entry_order_rejected":
        state["state"] = "entry_rejected"
        state["entry_order_terminal"] = True
        state["exit_intent_ready"] = False

    elif kind == "entry_fill":
        trade_id, _ = _confirmed_trade(event, "entry fill")
        gross = dec(event.get("gross_filled_shares"))
        net = dec(event.get("net_position_delta_shares"))
        if gross <= 0 or net <= 0:
            raise ValueError("entry fill requires positive gross/net shares")
        if trade_id not in state["entry_trade_ids"]:
            state["entry_trade_ids"].append(trade_id)
            state["entry_gross_filled_shares"] = str(
                dec(state["entry_gross_filled_shares"]) + gross
            )
            state["position_net_shares"] = str(
                dec(state["position_net_shares"]) + net
            )
            _record_cash_event(state, event_id)
        state["state"] = "entry_partially_filled"

    elif kind == "entry_order_terminal":
        state["entry_order_terminal"] = True
        if state["entry_trade_ids"]:
            if dec(state["position_net_shares"]) < dec(state["min_sell_size"]):
                state["state"] = "position_below_sell_minimum"
                state["halt_new_actions"] = True
                state["halt_reason"] = (
                    "confirmed_entry_net_shares_below_current_sell_minimum"
                )
            else:
                state["state"] = "position_open"
        else:
            state["state"] = "entry_unfilled"

    elif kind == "quote":
        reason, bid, bid_size, minimum = _quote_block_reason(state, event)
        state["last_best_bid"] = str(bid)
        state["last_bid_size"] = str(bid_size)
        state["last_tick_size"] = str(dec(event.get("tick_size")))
        state["last_quote_received_ms"] = str(dec(event.get("quote_received_ms")))
        state["min_sell_size"] = str(minimum)
        state["last_quote_block_reason"] = reason
        # Ask/mid fields are intentionally ignored. 0.98 semantics are based
        # only on a fresh executable best bid plus current rules/depth.
        state["exit_intent_ready"] = reason is None
        if reason is None:
            state["state"] = "exit_intent_ready"

    elif kind == "sell_order_accepted":
        order_id = str(event.get("order_id") or "")
        requested = dec(event.get("reserved_shares"))
        if not order_id or requested <= 0:
            raise ValueError(
                "accepted sell requires order_id and positive reserved_shares"
            )
        if requested > available_sell_shares(state):
            raise ValueError("sell reservation exceeds unreserved position")
        state["open_sell_orders"][order_id] = {
            "remaining_shares": str(requested)
        }
        state["exit_intent_ready"] = False
        state["state"] = "exit_order_accepted"

    elif kind == "sell_fill":
        order_id = str(event.get("order_id") or "")
        trade_id, _ = _confirmed_trade(event, "sell fill")
        shares = dec(event.get("filled_shares"))
        proceeds = dec(event.get("reported_proceeds_usd", "0"))
        order = state["open_sell_orders"].get(order_id)
        if not order or shares <= 0 or proceeds < 0:
            raise ValueError(
                "sell fill requires known order and positive shares"
            )
        remaining = dec(order["remaining_shares"])
        if (
            shares > remaining
            or shares > dec(state["position_net_shares"])
        ):
            raise ValueError("sell fill exceeds reserved or owned shares")
        if trade_id not in state["exit_trade_ids"]:
            state["exit_trade_ids"].append(trade_id)
            order["remaining_shares"] = str(remaining - shares)
            state["position_net_shares"] = str(
                dec(state["position_net_shares"]) - shares
            )
            state["reported_exit_proceeds_usd"] = str(
                dec(state["reported_exit_proceeds_usd"]) + proceeds
            )
            _record_cash_event(state, event_id)
        if dec(order["remaining_shares"]) == 0:
            state["open_sell_orders"].pop(order_id, None)
        state["cash_reusable"] = False
        state["state"] = (
            "exit_filled_cash_pending"
            if dec(state["position_net_shares"]) == 0
            else "exit_partially_filled"
        )
        _halt_if_stranded_position(state)

    elif kind == "sell_cancel_requested":
        order_id = str(event.get("order_id") or "")
        if order_id not in state["open_sell_orders"]:
            raise ValueError(
                "cancel request requires known open sell order"
            )
        # Reservation remains until an explicit cancel confirmation receipt.
        state["state"] = "exit_cancel_pending"

    elif kind == "sell_cancel_confirmed":
        order_id = str(event.get("order_id") or "")
        cancel_ref = str(event.get("cancel_reference") or "")
        if order_id not in state["open_sell_orders"] or not cancel_ref:
            raise ValueError(
                "cancel confirmation requires known order and cancel_reference"
            )
        state["open_sell_orders"].pop(order_id)
        state["state"] = (
            "position_open"
            if dec(state["position_net_shares"]) > 0
            else "exit_filled_cash_pending"
        )
        _halt_if_stranded_position(state)

    elif kind == "market_expired":
        state["exit_intent_ready"] = False
        if dec(state["position_net_shares"]) > 0:
            state["state"] = "settlement_pending"

    elif kind == "settlement_redeemable":
        resolution_ref = str(event.get("official_resolution_ref") or "")
        if dec(state["position_net_shares"]) <= 0 or not resolution_ref:
            raise ValueError(
                "redeemable state requires position and official resolution reference"
            )
        state["state"] = "redeemable"

    elif kind == "redeem_requested":
        if state["state"] != "redeemable":
            raise ValueError("redeem request requires redeemable state")
        state["state"] = "redeem_requested"

    elif kind == "redeem_confirmed":
        receipt, _ = _confirmed_chain_receipt(event, "redeem confirmation")
        state["redeem_receipt_id"] = receipt
        state["position_net_shares"] = "0"
        state["open_sell_orders"] = {}
        state["state"] = "redeem_cash_pending"
        state["cash_reusable"] = False
        _record_cash_event(state, event_id)

    elif kind == "settlement_finalized":
        receipt, _ = _confirmed_chain_receipt(event, "settlement finalization")
        state["settlement_receipt_id"] = receipt
        state["position_net_shares"] = str(
            dec(event.get("reconciled_position_shares", "0"))
        )
        if dec(state["position_net_shares"]) != 0:
            raise ValueError(
                "settlement finalization must reconcile the position to zero"
            )
        state["open_sell_orders"] = {}
        state["state"] = "settlement_cash_pending"
        state["cash_reusable"] = False
        _record_cash_event(state, event_id)

    elif kind == "cash_reconciled":
        if (
            dec(state["position_net_shares"]) != 0
            or reserved_sell_shares(state) != 0
        ):
            raise ValueError(
                "cash cannot be reusable while position or sell reservations remain"
            )
        expected_asset = str(state.get("expected_cash_asset") or "")
        observed_asset = str(event.get("cash_asset") or "")
        checkpoint_id = str(event.get("balance_checkpoint_id") or "")
        covered = {str(x) for x in (event.get("covered_event_ids") or [])}
        required = set(state.get("cash_affecting_event_ids") or [])
        reconciled_position = dec(event.get("reconciled_position_shares"))
        amount = dec(event.get("available_cash_amount"))
        if not expected_asset:
            raise ValueError(
                "expected_cash_asset must be configured before cash reconciliation"
            )
        if observed_asset != expected_asset:
            raise ValueError("cash checkpoint asset does not match episode")
        if not checkpoint_id:
            raise ValueError("cash reconciliation requires balance_checkpoint_id")
        if reconciled_position != 0:
            raise ValueError(
                "cash reconciliation requires a zero net-position checkpoint"
            )
        if not required.issubset(covered):
            raise ValueError(
                "cash checkpoint does not cover all cash-affecting events"
            )
        if amount < 0:
            raise ValueError("available cash cannot be negative")
        state["cash_reconciled"] = True
        state["cash_checkpoint_id"] = checkpoint_id
        state["cash_checkpoint_covered_event_ids"] = sorted(covered)
        state["reconciled_cash_asset"] = observed_asset
        state["reconciled_available_cash_amount"] = str(amount)

    elif kind == "reconciliation_unknown":
        state["state"] = "unknown_reconciliation"
        state["halt_new_actions"] = True
        state["halt_reason"] = str(
            event.get("reason") or "unknown_receipt_or_cash_state"
        )
        state["cash_reusable"] = False
        state["exit_intent_ready"] = False

    else:
        raise ValueError(f"unsupported event kind: {kind}")

    _recompute_completion(state)
    return state


def completed_roundtrip_count(episodes: list[dict]) -> int:
    """Count completed cash-reconciled episodes; split fills never count twice."""
    return sum(
        1 for state in episodes if bool(state.get("completed_roundtrip"))
    )


def state_digest(state: dict) -> str:
    """Deterministic local integrity hash; it does not authenticate exchange truth."""
    payload = json.dumps(
        state,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
