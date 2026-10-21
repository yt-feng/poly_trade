"""Assemble one authenticated taker row for the v2 compound ledger.

This adapter is deliberately offline.  It joins already normalized
authenticated CLOB lifecycle events, a canonical on-chain ``OrderFilled``
record, and caller-supplied settlement/cash/balance observations.  It never
fetches an account, opens an RPC connection, signs, submits, cancels, sells,
or redeems.  The resulting row still has to pass the ten-row reconciler.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


TOLERANCE = Decimal("0.000001")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _required(mapping: Mapping[str, Any], field: str) -> Any:
    value = mapping.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field}_required")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return result


def _same_decimal(left: Any, right: Any, field: str) -> None:
    if abs(_decimal(left, field) - _decimal(right, field)) > TOLERANCE:
        raise ValueError(f"{field}_mismatch")


def _copy_fields(
    target: dict[str, Any],
    source: Mapping[str, Any],
    *,
    reserved: set[str],
    name: str,
) -> None:
    for key, value in source.items():
        if key in reserved:
            raise ValueError(f"{name}_contains_reserved_field:{key}")
        target[key] = value


def assemble_taker_compound_row(
    *,
    order_event: Mapping[str, Any],
    trade_event: Mapping[str, Any],
    orderfilled_event: Mapping[str, Any],
    ledger_fields: Mapping[str, Any],
    settlement_fields: Mapping[str, Any],
    balance_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Join one taker BUY fill to independent v2 ledger evidence.

    ``ledger_fields`` must carry the sequence, ledger/chain identity, the
    ``OrderFilled`` event id, entry timing/amounts and balance arithmetic.
    Settlement and cash-release identifiers are copied from their dedicated
    groups and must be distinct.  All identity and economic joins are checked
    before the row is returned.
    """
    groups = (order_event, trade_event, orderfilled_event, ledger_fields,
              settlement_fields, balance_fields)
    if not all(isinstance(value, Mapping) for value in groups):
        raise ValueError("evidence_field_groups_must_be_objects")
    if order_event.get("kind") not in {None, "entry_order_accepted"}:
        raise ValueError("order_event_must_be_accepted")
    if _text(order_event.get("order_status")).upper() not in {
        "MATCHED", "ORDER_STATUS_MATCHED"
    }:
        raise ValueError("order_status_must_be_matched")
    if trade_event.get("kind") not in {None, "entry_fill"}:
        raise ValueError("trade_event_must_be_confirmed_fill")

    owner = _text(_required(trade_event, "owner"))
    trade_owner = _text(trade_event.get("trade_owner"))
    if trade_owner and trade_owner.lower() != owner.lower():
        raise ValueError("trade_owner_scope_mismatch")
    if _text(trade_event.get("execution_role")).upper() != "TAKER":
        raise ValueError("trade_execution_role_must_be_taker")
    if _text(trade_event.get("trader_side")).upper() != "TAKER":
        raise ValueError("trade_side_must_be_taker")
    if _text(trade_event.get("trade_status")).upper() not in {
        "CONFIRMED", "TRADE_STATUS_CONFIRMED"
    }:
        raise ValueError("trade_status_must_be_confirmed")
    if _text(trade_event.get("side")).upper() != "BUY":
        raise ValueError("taker_entry_side_must_be_buy")

    order_id = _text(_required(order_event, "order_id"))
    if _text(trade_event.get("taker_order_id")) != order_id:
        raise ValueError("taker_order_not_linked_to_trade")
    trade_id = _text(_required(trade_event, "trade_id"))
    transaction_ref = _text(_required(trade_event, "transaction_ref"))
    market_id = _text(_required(trade_event, "market_id"))
    asset_id = _text(_required(trade_event, "asset_id"))
    shares = _text(_required(trade_event, "shares"))
    price = _text(_required(trade_event, "price"))

    event_id = _text(_required(orderfilled_event, "event_id"))
    if _text(orderfilled_event.get("tx_hash")) != transaction_ref:
        raise ValueError("orderfilled_transaction_mismatch")
    if _text(orderfilled_event.get("market_id")) != market_id:
        raise ValueError("orderfilled_market_mismatch")
    if _text(orderfilled_event.get("side")).upper() != "BUY":
        raise ValueError("orderfilled_side_must_be_buy")
    if orderfilled_event.get("sign") != 1:
        raise ValueError("orderfilled_sign_must_be_buy")
    if _text(orderfilled_event.get("taker")).lower() != owner.lower():
        raise ValueError("orderfilled_taker_owner_mismatch")
    _same_decimal(orderfilled_event.get("size"), shares, "orderfilled_size")
    _same_decimal(orderfilled_event.get("price"), price, "orderfilled_price")

    reserved = {
        "owner", "trade_owner", "order_id", "order_status", "trade_id",
        "trade_status", "transaction_ref", "market_id", "asset_id",
        "shares", "price", "execution_role", "trader_side",
    }
    row: dict[str, Any] = {}
    _copy_fields(row, ledger_fields, reserved=reserved, name="ledger_fields")
    _copy_fields(row, settlement_fields, reserved=reserved, name="settlement_fields")
    _copy_fields(row, balance_fields, reserved=reserved, name="balance_fields")

    for field in (
        "sequence_no", "ledger_id", "chain_id", "previous_checkpoint_id",
        "orderfilled_event_id",
        "entry_ts", "cash_available_ts", "entry_cost", "fee", "payout",
        "released_cash_since_previous_entry", "balance_before",
        "balance_after", "balance_checkpoint_id", "cash_reconciled",
        "independent_checkpoint", "settlement_receipt_id",
        "cash_release_receipt_id",
        "settlement_status", "settlement_transaction_hash",
        "settlement_timestamp", "settlement_payout", "settlement_reverted",
        "cash_release_status", "cash_release_transaction_hash",
        "cash_release_timestamp", "cash_release_amount",
    ):
        _required(row, field)
    if _text(row["orderfilled_event_id"]) != event_id:
        raise ValueError("ledger_orderfilled_event_mismatch")
    if _text(row["settlement_receipt_id"]) == _text(row["cash_release_receipt_id"]):
        raise ValueError("cash_release_receipt_not_independent")
    if _text(row["settlement_status"]).upper() != "CONFIRMED":
        raise ValueError("settlement_status_must_be_confirmed")
    if type(row["settlement_reverted"]) is not bool:
        raise ValueError("settlement_reversion_status_required")
    if row["settlement_reverted"]:
        raise ValueError("settlement_must_not_be_reverted")
    if _text(row["settlement_transaction_hash"]) != transaction_ref:
        raise ValueError("settlement_transaction_mismatch")
    if _text(row["cash_release_status"]).upper() != "CONFIRMED":
        raise ValueError("cash_release_status_must_be_confirmed")
    if _text(row["cash_release_transaction_hash"]) == transaction_ref:
        raise ValueError("cash_release_transaction_must_be_independent")
    try:
        settlement_timestamp = int(str(row["settlement_timestamp"]))
        cash_release_timestamp = int(str(row["cash_release_timestamp"]))
        entry_timestamp = int(str(row["entry_ts"]))
        available_timestamp = int(str(row["cash_available_ts"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt_timestamps_must_be_integer") from exc
    if settlement_timestamp < entry_timestamp:
        raise ValueError("settlement_timestamp_before_entry")
    if cash_release_timestamp != available_timestamp:
        raise ValueError("cash_release_timestamp_mismatch")
    if cash_release_timestamp <= entry_timestamp:
        raise ValueError("cash_release_timestamp_must_follow_entry")
    _same_decimal(row["settlement_payout"], row["payout"], "settlement_payout")
    _same_decimal(row["cash_release_amount"], row["payout"], "cash_release_amount")
    _same_decimal(orderfilled_event.get("cash_amount"), row["entry_cost"], "entry_cost")

    row.update({
        "owner": owner,
        "trade_owner": trade_owner or owner,
        "order_id": order_id,
        "order_status": _text(order_event.get("order_status")).upper(),
        "trade_id": trade_id,
        "trade_status": _text(trade_event.get("trade_status")).upper(),
        "transaction_ref": transaction_ref,
        "market_id": market_id,
        "asset_id": asset_id,
        "shares": shares,
        "price": price,
        "execution_role": "TAKER",
        "trader_side": "TAKER",
        "orderfilled_event_id": event_id,
    })
    return row


__all__ = ["assemble_taker_compound_row"]
