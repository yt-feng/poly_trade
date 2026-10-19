"""Join captured maker order/trade frames into one compound-ledger row.

The adapter is intentionally offline.  It consumes events already normalized
by :mod:`polymarket_receipt_adapter` and caller-supplied settlement, rebate,
and cash-checkpoint fields.  It never opens a user stream, polls an account,
or submits an order.  The resulting row still has to pass
``validate_maker_compound_chain``; this layer only prevents ambiguous manual
field joins and preserves the official owner/maker/order/trade identities.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

try:
    from .h341_async_maker_bundle_link import link_confirmed_maker_trade
except ImportError:  # pragma: no cover - standalone repository tools.
    from h341_async_maker_bundle_link import link_confirmed_maker_trade


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _required(mapping: Mapping[str, Any], field: str) -> Any:
    value = mapping.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field}_required")
    return value


def _copy_fields(target: dict[str, Any], source: Mapping[str, Any], *, reserved: set[str], name: str) -> None:
    for key, value in source.items():
        if key in reserved:
            raise ValueError(f"{name}_contains_reserved_field:{key}")
        target[key] = value


def assemble_maker_compound_row(
    *,
    order_event: Mapping[str, Any],
    trade_event: Mapping[str, Any],
    ledger_fields: Mapping[str, Any],
    settlement_fields: Mapping[str, Any],
    rebate_fields: Mapping[str, Any],
    post_only_accepted: bool,
    order_updates: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one maker row from normalized lifecycle evidence.

    ``order_event`` is the authenticated order-update event and
    ``trade_event`` is the confirmed event emitted by ``adapt_trade``.  For a
    maker trade the account's order must appear in ``maker_order_ids``; the
    trade's ``taker_order_id`` is the counterparty order and cannot be used as
    the maker identity.  ``ledger_fields`` must include the onchain
    ``orderfilled_event_id`` and the independent balance/checkpoint fields.
    """
    if not isinstance(order_event, Mapping) or not isinstance(trade_event, Mapping):
        raise ValueError("lifecycle_events_must_be_objects")
    if not isinstance(ledger_fields, Mapping) or not isinstance(settlement_fields, Mapping) or not isinstance(rebate_fields, Mapping):
        raise ValueError("evidence_field_groups_must_be_objects")
    if type(post_only_accepted) is not bool:
        raise ValueError("post_only_acceptance_must_be_bool")
    if order_event.get("kind") not in {None, "entry_order_accepted"}:
        raise ValueError("order_event_must_be_accepted")
    if trade_event.get("kind") not in {None, "entry_fill"}:
        raise ValueError("trade_event_must_be_confirmed_fill")

    owner = _text(_required(trade_event, "owner"))
    trade_owner = _text(trade_event.get("trade_owner"))
    if trade_owner and trade_owner != owner:
        raise ValueError("trade_owner_scope_mismatch")
    if _text(trade_event.get("execution_role")).upper() != "MAKER":
        raise ValueError("trade_execution_role_must_be_maker")
    if _text(trade_event.get("trader_side")).upper() != "MAKER":
        raise ValueError("trade_side_must_be_maker")
    if _text(trade_event.get("trade_status")).upper() not in {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}:
        raise ValueError("trade_status_must_be_confirmed")
    fee_rate_bps = _text(trade_event.get("fee_rate_bps"))
    if not fee_rate_bps:
        raise ValueError("maker_fee_rate_required")
    if _text(trade_event.get("side")).upper() != "BUY":
        raise ValueError("maker_entry_side_must_be_buy")
    maker_address = _text(_required(trade_event, "maker_address"))
    trade_id = _text(_required(trade_event, "trade_id"))
    transaction_ref = _text(_required(trade_event, "transaction_ref"))
    order_id = _text(_required(order_event, "order_id"))
    maker_order_ids = {_text(value) for value in (trade_event.get("maker_order_ids") or []) if _text(value)}
    if order_id not in maker_order_ids:
        raise ValueError("maker_order_not_linked_to_trade")

    async_binding = None
    if order_updates is not None:
        async_binding = link_confirmed_maker_trade(order_updates, trade_event)
        if not async_binding["async_binding_ready"]:
            reason = async_binding["issues"][0] if async_binding["issues"] else "unknown"
            raise ValueError(f"async_trade_id_binding_failed:{reason}")

    reserved = {
        "owner", "maker_address", "trade_owner", "order_id", "order_status",
        "trade_id", "trade_status", "transaction_ref", "market_id", "asset_id",
        "shares", "price", "execution_role", "trader_side", "post_only_accepted",
    }
    row: dict[str, Any] = {}
    _copy_fields(row, ledger_fields, reserved=reserved, name="ledger_fields")
    _copy_fields(row, settlement_fields, reserved=reserved, name="settlement_fields")
    _copy_fields(row, rebate_fields, reserved=reserved, name="rebate_fields")
    _required(row, "sequence_no")
    _required(row, "ledger_id")
    _required(row, "chain_id")
    _required(row, "orderfilled_event_id")
    row.update({
        "owner": owner,
        "maker_address": maker_address,
        "trade_owner": trade_owner or owner,
        "order_id": order_id,
        "order_status": _text(order_event.get("order_status")).upper(),
        "trade_id": trade_id,
        "trade_status": _text(trade_event.get("trade_status")).upper(),
        "transaction_ref": transaction_ref,
        "market_id": _text(_required(trade_event, "market_id")),
        "asset_id": _text(_required(trade_event, "asset_id")),
        "shares": _text(_required(trade_event, "shares")),
        "price": _text(_required(trade_event, "price")),
        "fee_rate": fee_rate_bps,
        "fee": _text(row.get("fee", "0")),
        "execution_role": "MAKER",
        "trader_side": "MAKER",
        "post_only_accepted": post_only_accepted,
    })
    if async_binding is not None:
        # This is a binding status only; it is deliberately not a fill count.
        row["async_trade_id_binding_ready"] = True
    return row


__all__ = ["assemble_maker_compound_row"]
