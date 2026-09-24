"""Offline receipt/cash state contract for a future human-approved micro canary.

This module has no exchange client, wallet, signing, order submission, cancel,
sell, redeem, or balance-read capability. It only consumes externally supplied
receipts/observations and makes the accounting state explicit.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


def dec(value: Any) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return out


def new_episode(*, local_id: str, token_id: str, min_sell_size: Any, exit_trigger: Any = "0.98") -> dict:
    minimum = dec(min_sell_size)
    trigger = dec(exit_trigger)
    if not local_id or not token_id or minimum <= 0 or not Decimal("0") < trigger < Decimal("1"):
        raise ValueError("invalid episode identity or constraints")
    return {
        "schema": 1,
        "mode": "receipt_contract_no_order_capability",
        "local_id": str(local_id),
        "token_id": str(token_id),
        "state": "planned",
        "min_sell_size": str(minimum),
        "exit_trigger_best_bid": str(trigger),
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
        "cash_reconciled": False,
        "reconciled_available_cash_usd": None,
        "cash_reusable": False,
        "completed_roundtrip": False,
        "halt_new_actions": False,
        "halt_reason": None,
        "exit_intent_ready": False,
        "last_best_bid": None,
        "seen_event_ids": [],
        "orders_submitted_by_contract": False,
    }


def reserved_sell_shares(state: dict) -> Decimal:
    return sum((dec(v["remaining_shares"]) for v in state.get("open_sell_orders", {}).values()), Decimal("0"))


def available_sell_shares(state: dict) -> Decimal:
    return max(Decimal("0"), dec(state.get("position_net_shares", "0")) - reserved_sell_shares(state))


def _recompute_completion(state: dict) -> None:
    has_entry = bool(state["entry_trade_ids"])
    has_exit_or_settlement = bool(state["exit_trade_ids"] or state["settlement_receipt_id"] or state["redeem_receipt_id"])
    flat = dec(state["position_net_shares"]) == 0 and reserved_sell_shares(state) == 0
    done = has_entry and has_exit_or_settlement and flat and bool(state["cash_reconciled"])
    state["completed_roundtrip"] = done
    state["cash_reusable"] = done and not state["halt_new_actions"]
    if done:
        state["state"] = "cash_reconciled"


def _event_id(event: dict) -> str:
    value = str(event.get("event_id") or "")
    if not value:
        raise ValueError("event_id is required for idempotency")
    return value


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
        trade_id = str(event.get("trade_id") or "")
        gross = dec(event.get("gross_filled_shares"))
        net = dec(event.get("net_position_delta_shares"))
        if not trade_id or gross <= 0 or net <= 0:
            raise ValueError("entry fill requires positive gross/net shares and trade_id")
        if trade_id not in state["entry_trade_ids"]:
            state["entry_trade_ids"].append(trade_id)
            state["entry_gross_filled_shares"] = str(dec(state["entry_gross_filled_shares"]) + gross)
            state["position_net_shares"] = str(dec(state["position_net_shares"]) + net)
        state["state"] = "entry_partially_filled"

    elif kind == "entry_order_terminal":
        state["entry_order_terminal"] = True
        if state["entry_trade_ids"]:
            if dec(state["position_net_shares"]) < dec(state["min_sell_size"]):
                state["state"] = "position_below_sell_minimum"
                state["halt_new_actions"] = True
                state["halt_reason"] = "matched_entry_net_shares_below_current_sell_minimum"
            else:
                state["state"] = "position_open"
        else:
            state["state"] = "entry_unfilled"

    elif kind == "quote":
        bid = dec(event.get("best_bid"))
        if not Decimal("0") <= bid <= Decimal("1"):
            raise ValueError("invalid best bid")
        state["last_best_bid"] = str(bid)
        # Ask/mid fields are intentionally ignored. 0.98 semantics are based
        # only on executable best bid / later actual match receipts.
        eligible = (
            not state["halt_new_actions"]
            and state["entry_order_terminal"]
            and bool(state["entry_trade_ids"])
            and bid >= dec(state["exit_trigger_best_bid"])
            and available_sell_shares(state) >= dec(state["min_sell_size"])
        )
        state["exit_intent_ready"] = bool(eligible)
        if eligible:
            state["state"] = "exit_intent_ready"

    elif kind == "sell_order_accepted":
        order_id = str(event.get("order_id") or "")
        requested = dec(event.get("reserved_shares"))
        if not order_id or requested <= 0:
            raise ValueError("accepted sell requires order_id and positive reserved_shares")
        if requested > available_sell_shares(state):
            raise ValueError("sell reservation exceeds unreserved position")
        state["open_sell_orders"][order_id] = {"remaining_shares": str(requested)}
        state["exit_intent_ready"] = False
        state["state"] = "exit_order_accepted"

    elif kind == "sell_fill":
        order_id = str(event.get("order_id") or "")
        trade_id = str(event.get("trade_id") or "")
        shares = dec(event.get("filled_shares"))
        proceeds = dec(event.get("reported_proceeds_usd", "0"))
        order = state["open_sell_orders"].get(order_id)
        if not order or not trade_id or shares <= 0 or proceeds < 0:
            raise ValueError("sell fill requires known order, trade_id and positive shares")
        remaining = dec(order["remaining_shares"])
        if shares > remaining or shares > dec(state["position_net_shares"]):
            raise ValueError("sell fill exceeds reserved or owned shares")
        if trade_id not in state["exit_trade_ids"]:
            state["exit_trade_ids"].append(trade_id)
            order["remaining_shares"] = str(remaining - shares)
            state["position_net_shares"] = str(dec(state["position_net_shares"]) - shares)
            state["reported_exit_proceeds_usd"] = str(dec(state["reported_exit_proceeds_usd"]) + proceeds)
        if dec(order["remaining_shares"]) == 0:
            state["open_sell_orders"].pop(order_id, None)
        state["cash_reusable"] = False
        state["state"] = "exit_filled_cash_pending" if dec(state["position_net_shares"]) == 0 else "exit_partially_filled"

    elif kind == "sell_cancel_requested":
        order_id = str(event.get("order_id") or "")
        if order_id not in state["open_sell_orders"]:
            raise ValueError("cancel request requires known open sell order")
        # Reservation remains until an explicit cancel confirmation receipt.
        state["state"] = "exit_cancel_pending"

    elif kind == "sell_cancel_confirmed":
        order_id = str(event.get("order_id") or "")
        if order_id not in state["open_sell_orders"]:
            raise ValueError("cancel confirmation requires known open sell order")
        state["open_sell_orders"].pop(order_id)
        state["state"] = "position_open" if dec(state["position_net_shares"]) > 0 else "exit_filled_cash_pending"

    elif kind == "market_expired":
        state["exit_intent_ready"] = False
        if dec(state["position_net_shares"]) > 0:
            state["state"] = "settlement_pending"

    elif kind == "settlement_redeemable":
        if dec(state["position_net_shares"]) <= 0:
            raise ValueError("redeemable state requires an unresolved position")
        state["state"] = "redeemable"

    elif kind == "redeem_requested":
        if state["state"] != "redeemable":
            raise ValueError("redeem request requires redeemable state")
        state["state"] = "redeem_requested"

    elif kind == "redeem_confirmed":
        receipt = str(event.get("receipt_id") or "")
        if not receipt:
            raise ValueError("redeem confirmation requires receipt_id")
        state["redeem_receipt_id"] = receipt
        state["position_net_shares"] = "0"
        state["open_sell_orders"] = {}
        state["state"] = "redeem_cash_pending"
        state["cash_reusable"] = False

    elif kind == "settlement_finalized":
        receipt = str(event.get("receipt_id") or "")
        if not receipt:
            raise ValueError("settlement finalization requires receipt_id")
        state["settlement_receipt_id"] = receipt
        state["position_net_shares"] = str(dec(event.get("reconciled_position_shares", "0")))
        if dec(state["position_net_shares"]) != 0:
            raise ValueError("settlement finalization must reconcile the position to zero")
        state["open_sell_orders"] = {}
        state["state"] = "settlement_cash_pending"
        state["cash_reusable"] = False

    elif kind == "cash_reconciled":
        if dec(state["position_net_shares"]) != 0 or reserved_sell_shares(state) != 0:
            raise ValueError("cash cannot be reusable while position or sell reservations remain")
        amount = dec(event.get("available_cash_usd"))
        if amount < 0:
            raise ValueError("available cash cannot be negative")
        state["cash_reconciled"] = True
        state["reconciled_available_cash_usd"] = str(amount)

    elif kind == "reconciliation_unknown":
        state["state"] = "unknown_reconciliation"
        state["halt_new_actions"] = True
        state["halt_reason"] = str(event.get("reason") or "unknown_receipt_or_cash_state")
        state["cash_reusable"] = False
        state["exit_intent_ready"] = False

    else:
        raise ValueError(f"unsupported event kind: {kind}")

    _recompute_completion(state)
    return state


def completed_roundtrip_count(episodes: list[dict]) -> int:
    """Count completed cash-reconciled episodes; split fills never count twice."""
    return sum(1 for state in episodes if bool(state.get("completed_roundtrip")))
