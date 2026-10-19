"""Bind an asynchronous maker trade to the order updates that preserved it.

The current CLOB can return ``tradeIDs`` before a transaction hash is known.
That response is useful identity evidence, but it is not a fill.  This pure
contract joins a later confirmed maker trade to the earlier order-update
history and fails closed when the trade ID, maker order, or owner scope cannot
be followed.  It does not authenticate data, release cash, or count a Canary
fill.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


_CONFIRMED = frozenset({"CONFIRMED", "TRADE_STATUS_CONFIRMED"})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _ids(value: Any, field: str) -> tuple[list[str], list[str]]:
    if not isinstance(value, list):
        return [], [f"{field}_must_be_list"]
    result: list[str] = []
    issues: list[str] = []
    for item in value:
        value_text = _text(item)
        if not value_text:
            issues.append(f"{field}_contains_blank_id")
        else:
            result.append(value_text)
    return result, issues


def link_confirmed_maker_trade(
    order_updates: Iterable[Mapping[str, Any]],
    confirmed_trade: Mapping[str, Any],
) -> dict[str, Any]:
    """Check the async identity chain without declaring a live fill.

    At least one order update must explicitly carry the confirmed trade ID.
    An initial update with an empty list is allowed; a later update must close
    the identity chain.  The maker order ID and owner are checked when the
    corresponding fields are supplied by the normalized trade.
    """
    issues: list[str] = []
    updates = list(order_updates)
    if not updates:
        issues.append("order_updates_required")
    if not isinstance(confirmed_trade, Mapping):
        issues.append("confirmed_trade_must_be_object")
        return {
            "schema_version": 1,
            "async_binding_ready": False,
            "confirmed_fill_event_preserved": False,
            "fill_count": 0,
            "issues": issues,
        }

    trade_id = _text(_first(confirmed_trade, "trade_id", "tradeID", "id"))
    if not trade_id:
        issues.append("confirmed_trade_id_required")
    status = _text(_first(confirmed_trade, "trade_status", "status")).upper()
    if status not in _CONFIRMED:
        issues.append("trade_status_must_be_confirmed")
    transaction_ref = _text(
        _first(confirmed_trade, "transaction_ref", "transaction_hash", "transactionHash")
    )
    if not transaction_ref:
        issues.append("transaction_ref_required")

    owner = _text(confirmed_trade.get("owner"))
    trade_owner = _text(confirmed_trade.get("trade_owner"))
    if trade_owner and owner and trade_owner != owner:
        issues.append("trade_owner_scope_mismatch")

    order_ids: set[str] = set()
    observed_trade_ids: set[str] = set()
    trade_id_field_seen = False
    for index, update in enumerate(updates):
        if not isinstance(update, Mapping):
            issues.append(f"order_update_{index}_must_be_object")
            continue
        order_id = _text(_first(update, "order_id", "id"))
        if order_id:
            order_ids.add(order_id)
        if "trade_ids" in update or "tradeIDs" in update:
            trade_id_field_seen = True
            values, value_issues = _ids(
                _first(update, "trade_ids", "tradeIDs"),
                f"order_update_{index}_trade_ids",
            )
            observed_trade_ids.update(values)
            issues.extend(value_issues)
        update_owner = _text(update.get("owner"))
        if owner and update_owner and update_owner != owner:
            issues.append(f"order_update_{index}_owner_scope_mismatch")

    if not trade_id_field_seen:
        issues.append("trade_ids_not_observed_in_order_updates")
    elif trade_id and trade_id not in observed_trade_ids:
        issues.append("confirmed_trade_id_not_preserved_by_order_updates")

    maker_order_ids, maker_order_issues = _ids(
        _first(confirmed_trade, "maker_order_ids", "makerOrderIds") or [],
        "maker_order_ids",
    )
    issues.extend(maker_order_issues)
    if not maker_order_ids:
        issues.append("maker_order_ids_required")
    elif order_ids and not order_ids.intersection(maker_order_ids):
        issues.append("maker_order_not_linked_to_order_updates")

    ready = not issues
    return {
        "schema_version": 1,
        "async_binding_ready": ready,
        "confirmed_fill_event_preserved": ready,
        "fill_count": 0,
        "trade_id": trade_id or None,
        "transaction_ref_present": bool(transaction_ref),
        "observed_trade_ids": sorted(observed_trade_ids),
        "observed_order_ids": sorted(order_ids),
        "issues": issues,
    }


__all__ = ["link_confirmed_maker_trade"]


def audit() -> dict[str, Any]:
    """Run the deterministic boundary checks used by the sealed report."""
    base_order = {
        "kind": "entry_order_accepted",
        "order_id": "maker-order-1",
        "owner": "owner-1",
    }
    confirmed = {
        "kind": "entry_fill",
        "owner": "owner-1",
        "trade_owner": "owner-1",
        "trade_id": "trade-1",
        "transaction_ref": "0xtx-1",
        "trade_status": "TRADE_STATUS_CONFIRMED",
        "maker_order_ids": ["maker-order-1"],
    }
    ready = link_confirmed_maker_trade(
        [{**base_order, "trade_ids": []}, {**base_order, "trade_ids": ["trade-1"]}],
        confirmed,
    )
    mismatch = link_confirmed_maker_trade(
        [{**base_order, "trade_ids": ["trade-other"]}], confirmed
    )
    missing_transaction = link_confirmed_maker_trade(
        [{**base_order, "trade_ids": ["trade-1"]}],
        {**confirmed, "transaction_ref": ""},
    )
    return {
        "format": "h341-async-maker-bundle-link-v1",
        "hypothesis_id": "H341-async-maker-bundle-link",
        "source_scope": "local pure async order/trade identity contract; no market data, account access, orders or settlement access",
        "initial_trade_ids_empty_is_non_fill": True,
        "matching_later_trade_id_binds": bool(ready["async_binding_ready"]),
        "mismatched_trade_id_rejected": not mismatch["async_binding_ready"],
        "missing_transaction_rejected": not missing_transaction["async_binding_ready"],
        "fill_count": 0,
        "external_owner_settlement_cash_still_required": True,
        "decision": "async_trade_id_binding_ready_but_external_receipts_missing",
        "root_cause": "The prior adapter preserved tradeIDs but did not require the later confirmed maker trade ID to be present in the captured order-update history. H341 closes that identity join while keeping order acceptance and binding distinct from a fill.",
        "strategy_evaluated": False,
        "paper_only": True,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(audit(), ensure_ascii=False, sort_keys=True))
