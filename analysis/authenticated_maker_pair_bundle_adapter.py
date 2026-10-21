"""Assemble two normalized authenticated maker legs into one pair event.

The adapter consumes captured order/trade frames and caller-supplied
settlement/ledger fields.  It performs no network, wallet, signing, order or
balance operation.  Its output is only a structural input to
``validate_authenticated_maker_pair_bundle``; it cannot authenticate the
source or count a Canary by itself.
"""
from __future__ import annotations

from typing import Any, Mapping


_CONFIRMED = {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}


def _text(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field}_required")
    return str(value).strip()


def _first(mapping: Mapping[str, Any], field: str, *aliases: str) -> Any:
    for name in (field, *aliases):
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _timestamp(mapping: Mapping[str, Any], field: str, *aliases: str) -> int:
    value = _first(mapping, field, *aliases)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_integer_timestamp") from exc


def _leg(
    *,
    prefix: str,
    order_event: Mapping[str, Any],
    trade_event: Mapping[str, Any],
    owner: str,
    market_id: str,
    entry_ts: int,
) -> dict[str, Any]:
    order_id = _text(_first(order_event, "order_id", "id"), f"{prefix}_order_id")
    order_owner = order_event.get("owner")
    if order_owner is not None and _text(order_owner, f"{prefix}_order_owner") != owner:
        raise ValueError(f"{prefix}_owner_scope_mismatch")
    trade_id = _text(_first(trade_event, "trade_id", "tradeID", "id"), f"{prefix}_trade_id")
    tx_ref = _text(
        _first(trade_event, "transaction_ref", "transaction_hash", "transactionHash"),
        f"{prefix}_transaction_ref",
    )
    trade_owner = _text(trade_event.get("owner"), f"{prefix}_trade_owner")
    if trade_owner != owner:
        raise ValueError(f"{prefix}_owner_scope_mismatch")
    if _text(trade_event.get("trade_owner") or owner, f"{prefix}_trade_owner") != owner:
        raise ValueError(f"{prefix}_trade_owner_scope_mismatch")
    trade_market = _text(
        _first(trade_event, "market_id", "condition_id", "conditionId"),
        f"{prefix}_market_id",
    )
    if trade_market != market_id:
        raise ValueError(f"{prefix}_market_mismatch")
    asset_id = _text(
        _first(trade_event, "asset_id", "assetId", "token_id", "tokenId"),
        f"{prefix}_asset_id",
    )
    order_type = _text(_first(order_event, "order_type", "orderType"), f"{prefix}_order_type").upper()
    order_status = _text(_first(order_event, "order_status", "status"), f"{prefix}_order_status").upper()
    trade_status = _text(
        _first(trade_event, "trade_status", "status"), f"{prefix}_trade_status"
    ).upper()
    trader_side = _text(
        _first(trade_event, "trader_side", "traderSide"), f"{prefix}_trader_side"
    ).upper()
    execution_role = _text(trade_event.get("execution_role"), f"{prefix}_execution_role").upper()
    side = _text(trade_event.get("side"), f"{prefix}_side").upper()
    if order_type not in {"GTC", "GTD"}:
        raise ValueError(f"{prefix}_order_type_must_be_gtc_or_gtd")
    if order_event.get("post_only") is not True and order_event.get("post_only_accepted") is not True:
        raise ValueError(f"{prefix}_post_only_required")
    if order_status != "MATCHED":
        raise ValueError(f"{prefix}_order_match_status_required")
    if trade_status not in _CONFIRMED:
        raise ValueError(f"{prefix}_trade_confirmation_required")
    if trader_side != "MAKER" or execution_role != "MAKER":
        raise ValueError(f"{prefix}_trader_side_required")
    if side != "BUY":
        raise ValueError(f"{prefix}_entry_side_must_be_buy")
    maker_order_ids = trade_event.get("maker_order_ids", trade_event.get("makerOrderIds", []))
    if not isinstance(maker_order_ids, list) or order_id not in {str(value).strip() for value in maker_order_ids}:
        raise ValueError(f"{prefix}_maker_order_not_linked_to_trade")
    acceptance_ts = _timestamp(order_event, "order_acceptance_ts", "accepted_at", "timestamp")
    matched_ts = _timestamp(trade_event, "trade_matched_ts", "matched_at", "matchedAt", "timestamp")
    if acceptance_ts > entry_ts:
        raise ValueError(f"{prefix}_order_acceptance_after_entry")
    if matched_ts > entry_ts:
        raise ValueError(f"{prefix}_trade_match_after_entry")
    fee_rate_bps = _text(
        _first(trade_event, "fee_rate_bps", "feeRateBps"), f"{prefix}_fee_rate_bps"
    )
    if fee_rate_bps not in {"0", "0.0", "0.00"}:
        raise ValueError("maker_fee_not_zero")
    shares = _text(_first(trade_event, "shares", "size"), f"{prefix}_shares")
    price = _text(trade_event.get("price"), f"{prefix}_price")
    return {
        f"{prefix}_order_id": order_id,
        f"{prefix}_trade_id": trade_id,
        f"{prefix}_transaction_ref": tx_ref,
        f"{prefix}_order_type": order_type,
        f"{prefix}_post_only": True,
        f"{prefix}_order_status": order_status,
        f"{prefix}_trade_status": trade_status,
        f"{prefix}_trader_side": trader_side,
        f"{prefix}_order_acceptance_ts": acceptance_ts,
        f"{prefix}_trade_matched_ts": matched_ts,
        f"{prefix}_fee_rate_bps": fee_rate_bps,
        f"{prefix}_shares": shares,
        f"{prefix}_price": price,
        f"{prefix}_asset_id": asset_id,
    }


def assemble_authenticated_maker_pair_event(
    *,
    up_order_event: Mapping[str, Any],
    up_trade_event: Mapping[str, Any],
    down_order_event: Mapping[str, Any],
    down_trade_event: Mapping[str, Any],
    pair_fields: Mapping[str, Any],
    settlement_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Join two captured maker legs and the pair's ledger/settlement fields."""
    groups = {
        "up_order_event": up_order_event,
        "up_trade_event": up_trade_event,
        "down_order_event": down_order_event,
        "down_trade_event": down_trade_event,
        "pair_fields": pair_fields,
        "settlement_fields": settlement_fields,
    }
    for name, value in groups.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"{name}_must_be_object")
    owner = _text(pair_fields.get("owner"), "owner")
    ledger_id = _text(pair_fields.get("ledger_id"), "ledger_id")
    chain_id = _text(pair_fields.get("chain_id"), "chain_id")
    market_id = _text(pair_fields.get("market_id"), "market_id")
    pair_id = _text(pair_fields.get("pair_id"), "pair_id")
    entry_ts = _timestamp(pair_fields, "entry_ts")
    up = _leg(prefix="up", order_event=up_order_event, trade_event=up_trade_event, owner=owner, market_id=market_id, entry_ts=entry_ts)
    down = _leg(prefix="down", order_event=down_order_event, trade_event=down_trade_event, owner=owner, market_id=market_id, entry_ts=entry_ts)
    if up["up_asset_id"] == down["down_asset_id"]:
        raise ValueError("paired_assets_must_differ")
    merged = dict(pair_fields)
    merged.update({
        "schema": "authenticated-maker-pair-event-v1",
        "owner": owner,
        "ledger_id": ledger_id,
        "chain_id": chain_id,
        "market_id": market_id,
        "pair_id": pair_id,
        "status": "confirmed",
        **up,
        **down,
    })
    for key, value in settlement_fields.items():
        if key in merged and merged[key] != value:
            raise ValueError(f"settlement_field_conflicts_with_pair:{key}")
        merged[key] = value
    for field in ("merge_receipt_id", "settlement_receipt_id", "cash_release_receipt_id"):
        _text(merged.get(field), field)
    return merged


__all__ = ["assemble_authenticated_maker_pair_event"]

