"""Assemble one authenticated taker row for the v2 compound ledger.

This adapter is deliberately offline.  It joins already normalized
authenticated CLOB lifecycle events, a canonical on-chain ``OrderFilled``
record, and caller-supplied settlement/cash/balance observations.  It never
fetches an account, opens an RPC connection, signs, submits, cancels, sells,
or redeems.  The resulting row still has to pass the ten-row reconciler.
"""
from __future__ import annotations

import hashlib
import json
import re
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


def _canonical_strategy_hash(decision: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(decision), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _required_text(mapping: Mapping[str, Any], field: str) -> str:
    value = _required(mapping, field)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field}_required")
    return text


def _required_int(mapping: Mapping[str, Any], field: str) -> int:
    value = _required(mapping, field)
    if type(value) is not int:
        raise ValueError(f"{field}_must_be_integer")
    return value


def assemble_h600_taker_compound_row(
    *,
    order_event: Mapping[str, Any],
    trade_event: Mapping[str, Any],
    orderfilled_event: Mapping[str, Any],
    ledger_fields: Mapping[str, Any],
    settlement_fields: Mapping[str, Any],
    balance_fields: Mapping[str, Any],
    source_evidence: Mapping[str, Any],
    depth_evidence: Mapping[str, Any],
    strategy_decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble one H600 row with execution-time provenance attached.

    The older adapter joins account, OrderFilled, settlement and balance
    groups.  This strict route also requires the source and depth records that
    were visible before entry and the exact online decision trace.  It is an
    offline assembler: callers provide already captured evidence and this
    function never opens a socket, reads a wallet, or submits an order.
    """
    groups = (source_evidence, depth_evidence, strategy_decision)
    if not all(isinstance(value, Mapping) for value in groups):
        raise ValueError("execution_provenance_groups_must_be_objects")
    row = assemble_taker_compound_row(
        order_event=order_event,
        trade_event=trade_event,
        orderfilled_event=orderfilled_event,
        ledger_fields=ledger_fields,
        settlement_fields=settlement_fields,
        balance_fields=balance_fields,
    )
    market_id = _required_text(row, "market_id")
    try:
        entry_ts = int(str(_required(row, "entry_ts")))
    except (TypeError, ValueError) as exc:
        raise ValueError("entry_ts_must_be_integer") from exc

    source_market = _required_text(source_evidence, "market_id")
    if source_market != market_id:
        raise ValueError("source_market_mismatch")
    source_seq = _required_int(source_evidence, "seq")
    if source_seq < 0:
        raise ValueError("source_seq_must_be_nonnegative")
    source_hash = _required_text(source_evidence, "raw_payload_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("source_raw_payload_sha256_invalid")
    source_event_ms = _required_int(source_evidence, "event_ms")
    if source_event_ms > entry_ts:
        raise ValueError("source_event_after_entry")

    depth_market = _required_text(depth_evidence, "market_id")
    if depth_market != market_id:
        raise ValueError("depth_market_mismatch")
    depth_seq = _required_int(depth_evidence, "seq")
    if depth_seq < 0:
        raise ValueError("depth_seq_must_be_nonnegative")
    depth_hash = _required_text(depth_evidence, "raw_payload_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", depth_hash):
        raise ValueError("depth_raw_payload_sha256_invalid")
    depth_event_ms = _required_int(depth_evidence, "event_ms")
    depth_received_ms = _required_int(depth_evidence, "received_ms")
    if depth_event_ms > entry_ts or depth_received_ms > entry_ts:
        raise ValueError("depth_snapshot_after_entry")
    position_side = _required_text(depth_evidence, "position_side").lower()
    if position_side not in {"up", "down"}:
        raise ValueError("depth_position_side_invalid")

    required_trace = (
        "decision_ref", "sequence_no", "cash_fraction", "position_side",
        "entry_sell_depth_shares", "depth_capacity_ratio", "favorite_ask_price",
    )
    for field in required_trace:
        _required(strategy_decision, field)
    trace = dict(strategy_decision)
    if set(trace) != set(required_trace):
        raise ValueError("strategy_decision_trace_invalid")
    if type(trace["sequence_no"]) is not int or trace["sequence_no"] != row["sequence_no"]:
        raise ValueError("strategy_sequence_mismatch")
    if str(trace["position_side"]).strip().lower() != position_side:
        raise ValueError("strategy_position_side_mismatch")
    if not _required_text(trace, "decision_ref"):
        raise ValueError("strategy_decision_ref_required")

    row.update({
        "entry_ts_ms": entry_ts,
        "cash_available_ts_ms": int(str(_required(row, "cash_available_ts"))),
        "source_ref": {
            "market_id": market_id,
            "capture_id": _required_text(source_evidence, "capture_id"),
            "seq": source_seq,
            "raw_payload_sha256": source_hash,
            "event_ms": source_event_ms,
        },
        "depth_ref": {
            "snapshot_id": _required_text(depth_evidence, "snapshot_id"),
            "capture_id": _required_text(depth_evidence, "capture_id"),
            "seq": depth_seq,
            "raw_payload_sha256": depth_hash,
            "event_ms": depth_event_ms,
        },
        "strategy_decision_ref": _required_text(trace, "decision_ref"),
        "strategy_decision_sha256": _canonical_strategy_hash(trace),
        "position_side": position_side,
    })
    return row


__all__ = ["assemble_taker_compound_row", "assemble_h600_taker_compound_row"]
