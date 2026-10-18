"""Pure adapter for authenticated Polymarket CLOB order/trade receipts.

This module only validates already supplied payloads.  It has no HTTP client,
wallet, signing, submit, cancel, sell, redeem, or balance-reading capability.
The output is deliberately shaped for :mod:`canary_receipt_state`, so an order
being accepted or matched cannot be mistaken for a confirmed fill or reusable
cash.

The CLOB uses share quantities.  ``size_matched`` is retained as a matched
share total; it is never treated as the current position balance.  A confirmed
entry event is emitted only for an authenticated account trade with a
``TRADE_STATUS_CONFIRMED`` status, transaction hash, and both match/update
timestamps.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


ORDER_STATUSES = frozenset(
    {
        "LIVE",
        "INVALID",
        "CANCELED_MARKET_RESOLVED",
        "CANCELED",
        "MATCHED",
        # Current authenticated user-stream payloads also expose these
        # non-terminal order states.
        "DELAYED",
        "UNMATCHED",
    }
)
TERMINAL_ORDER_STATUSES = frozenset(
    {"INVALID", "CANCELED_MARKET_RESOLVED", "CANCELED", "MATCHED"}
)
CONFIRMED_TRADE_STATUS = "TRADE_STATUS_CONFIRMED"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _pick(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _expected_issue(
    canonical: dict[str, Any], expected: dict[str, Any], field: str, actual_key: str
) -> dict[str, Any] | None:
    aliases = {
        "market_id": ("market_id", "condition_id", "conditionId", "market"),
        "token_id": ("token_id", "asset_id", "assetId", "tokenId"),
        "owner": ("owner",),
    }
    expected_value = _pick(expected, *aliases.get(field, (field,)))
    if expected_value in (None, ""):
        return None
    actual_value = _text(canonical.get(actual_key))
    if actual_value != _text(expected_value):
        return _issue(
            "IDENTITY_MISMATCH",
            f"{actual_key} does not match the frozen episode identity",
            field=field,
            expected=_text(expected_value),
            actual=actual_value,
        )
    return None


def _order_canonical(payload: dict[str, Any]) -> dict[str, Any]:
    associate = _pick(payload, "associate_trades", "associateTrades", default=[])
    if not isinstance(associate, list):
        raise ValueError("associate_trades must be a list")
    trade_ids = _pick(payload, "trade_ids", "tradeIDs", default=[])
    if not isinstance(trade_ids, list):
        raise ValueError("trade_ids must be a list")
    return {
        "id": _text(_pick(payload, "id")),
        "market_id": _text(_pick(payload, "market", "conditionId", "condition_id")),
        "asset_id": _text(_pick(payload, "asset_id", "assetId", "token_id", "tokenId")),
        "owner": _text(_pick(payload, "owner")),
        "side": _text(_pick(payload, "side")).upper(),
        "original_size": _text(_pick(payload, "original_size", "originalSize")),
        "size_matched": _text(_pick(payload, "size_matched", "sizeMatched", default="0")),
        "status": _text(_pick(payload, "status")).upper(),
        "associate_trades": associate,
        # Async order responses may expose trade IDs before the transaction
        # hash is available. Preserve them for a later authenticated trade
        # update; they never establish a fill by themselves.
        "trade_ids": [_text(value) for value in trade_ids if _text(value)],
    }


def _trade_canonical(payload: dict[str, Any]) -> dict[str, Any]:
    maker_orders = _pick(payload, "maker_orders", "makerOrders", default=[])
    if not isinstance(maker_orders, list):
        raise ValueError("maker_orders must be a list")
    return {
        "id": _text(_pick(payload, "id")),
        "market_id": _text(_pick(payload, "market", "conditionId", "condition_id")),
        "asset_id": _text(_pick(payload, "asset_id", "assetId", "token_id", "tokenId")),
        "owner": _text(_pick(payload, "owner")),
        "taker_order_id": _text(_pick(payload, "taker_order_id", "takerOrderId")),
        "side": _text(_pick(payload, "side")).upper(),
        "size": _text(_pick(payload, "size")),
        "status": _text(_pick(payload, "status")).upper(),
        "trader_side": _text(_pick(payload, "trader_side", "traderSide")).upper(),
        "fee_rate_bps": _text(_pick(payload, "fee_rate_bps", "feeRateBps")),
        "maker_address": _text(_pick(payload, "maker_address", "makerAddress")),
        "trade_owner": _text(_pick(payload, "trade_owner", "tradeOwner")),
        "transaction_hash": _text(
            _pick(payload, "transaction_hash", "transactionHash", "transaction_ref")
        ),
        "match_time": _text(_pick(payload, "match_time", "matchedAt", "matched_at")),
        "last_update": _text(_pick(payload, "last_update", "updatedAt", "updated_at")),
        "maker_orders": maker_orders,
    }


def _identity_issues(canonical: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for field, actual_key in (
        ("market_id", "market_id"),
        ("token_id", "asset_id"),
        ("owner", "owner"),
    ):
        found = _expected_issue(canonical, expected, field, actual_key)
        if found:
            issues.append(found)
    if not canonical["owner"]:
        issues.append(
            _issue(
                "MISSING_OWNER_SCOPE",
                "an account receipt must include authenticated owner scope",
            )
        )
    return issues


def adapt_order(payload: dict[str, Any], *, expected: dict[str, Any]) -> dict[str, Any]:
    """Validate one authenticated CLOB order snapshot and emit state events."""
    result: dict[str, Any] = {"events": [], "issues": [], "order": None}
    if not isinstance(payload, dict):
        result["issues"].append(_issue("INVALID_ORDER_PAYLOAD", "order payload must be an object"))
        return result
    try:
        order = _order_canonical(payload)
    except ValueError as exc:
        result["issues"].append(_issue("INVALID_ORDER_PAYLOAD", str(exc)))
        return result
    result["order"] = order
    issues = _identity_issues(order, expected)
    if not order["id"]:
        issues.append(_issue("MISSING_ORDER_ID", "order id is required"))
    if order["side"] != "BUY":
        issues.append(_issue("UNSUPPORTED_ORDER_SIDE", "entry adapter only accepts BUY orders", side=order["side"]))
    if order["status"] not in ORDER_STATUSES:
        issues.append(_issue("UNKNOWN_ORDER_STATUS", "order status is outside the documented enum", status=order["status"]))
    try:
        original = _decimal(order["original_size"], "original_size")
        matched = _decimal(order["size_matched"], "size_matched")
        if original <= 0 or matched < 0 or matched > original:
            issues.append(_issue("INVALID_ORDER_QUANTITY", "matched shares must be within the original share quantity"))
    except ValueError as exc:
        issues.append(_issue("INVALID_ORDER_QUANTITY", str(exc)))
    result["issues"].extend(issues)
    if issues:
        return result

    order_id = order["id"]
    if order["status"] == "INVALID":
        result["events"].append(
            {
                "event_id": f"order:{order_id}:rejected",
                "kind": "entry_order_rejected",
                "order_id": order_id,
                "order_status": order["status"],
                "evidence_source": "authenticated_clob_order",
            }
        )
        return result

    result["events"].append(
        {
            "event_id": f"order:{order_id}:accepted",
            "kind": "entry_order_accepted",
            "order_id": order_id,
            "order_status": order["status"],
            "original_size_shares": order["original_size"],
            "size_matched_shares": order["size_matched"],
            "trade_ids": list(order["trade_ids"]),
            "evidence_source": "authenticated_clob_order",
        }
    )
    if order["status"] in TERMINAL_ORDER_STATUSES:
        result["events"].append(
            {
                "event_id": f"order:{order_id}:terminal:{order['status']}",
                "kind": "entry_order_terminal",
                "order_id": order_id,
                "order_status": order["status"],
                "evidence_source": "authenticated_clob_order",
            }
        )
    return result


def _maker_order_ids(values: Iterable[Any]) -> set[str]:
    ids: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = _pick(value, "order_id", "orderId", "id")
        text = _text(value)
        if text:
            ids.add(text)
    return ids


def adapt_trade(
    payload: dict[str, Any], *, expected: dict[str, Any], order_id: str | None = None,
    required_trader_side: str | None = None,
) -> dict[str, Any]:
    """Validate one authenticated account trade and emit a confirmed fill only."""
    result: dict[str, Any] = {"events": [], "issues": [], "trade": None}
    if not isinstance(payload, dict):
        result["issues"].append(_issue("INVALID_TRADE_PAYLOAD", "trade payload must be an object"))
        return result
    try:
        trade = _trade_canonical(payload)
    except ValueError as exc:
        result["issues"].append(_issue("INVALID_TRADE_PAYLOAD", str(exc)))
        return result
    result["trade"] = trade
    issues = _identity_issues(trade, expected)
    if not trade["id"]:
        issues.append(_issue("MISSING_TRADE_ID", "trade id is required"))
    if trade["side"] != "BUY":
        issues.append(_issue("UNSUPPORTED_TRADE_SIDE", "entry adapter only accepts BUY trades", side=trade["side"]))
    required_side = _text(required_trader_side).upper()
    if required_side and required_side not in {"MAKER", "TAKER"}:
        issues.append(_issue("INVALID_REQUIRED_TRADER_SIDE", "required trader side must be MAKER or TAKER"))
    elif required_side:
        if trade["trader_side"] != required_side:
            issues.append(
                _issue(
                    "TRADER_SIDE_MISMATCH",
                    "authenticated trade does not have the required maker/taker role",
                    expected=required_side,
                    actual=trade["trader_side"],
                )
            )
        if required_side == "MAKER" and trade["fee_rate_bps"] not in {"0", "0.0", "0.00"}:
            issues.append(
                _issue(
                    "MAKER_FEE_NOT_ZERO",
                    "maker evidence must carry a zero fee rate",
                    fee_rate_bps=trade["fee_rate_bps"],
                )
            )
    if not trade["taker_order_id"]:
        issues.append(_issue("MISSING_TAKER_ORDER_ID", "authenticated trade must include taker_order_id"))
    try:
        if _decimal(trade["size"], "size") <= 0:
            issues.append(_issue("INVALID_TRADE_QUANTITY", "trade size must be positive"))
    except ValueError as exc:
        issues.append(_issue("INVALID_TRADE_QUANTITY", str(exc)))
    if trade["status"] != CONFIRMED_TRADE_STATUS:
        issues.append(
            _issue(
                "TRADE_NOT_CONFIRMED",
                "a matched or pending trade cannot count as a confirmed fill",
                status=trade["status"],
            )
        )
    if not trade["transaction_hash"]:
        issues.append(_issue("MISSING_TRANSACTION_HASH", "confirmed fill requires a transaction hash"))
    if not trade["match_time"] or not trade["last_update"]:
        issues.append(_issue("MISSING_TRADE_TIMESTAMPS", "confirmed fill requires match and update timestamps"))
    expected_order = _text(order_id or _pick(expected, "order_id"))
    if expected_order:
        related = {trade["taker_order_id"]} | _maker_order_ids(trade["maker_orders"])
        if expected_order not in related:
            issues.append(
                _issue(
                    "ORDER_TRADE_MISMATCH",
                    "trade is not associated with the expected order",
                    expected=expected_order,
                    related=sorted(related),
                )
            )
    result["issues"].extend(issues)
    if issues:
        return result

    result["events"].append(
        {
            "event_id": f"trade:{trade['id']}:confirmed",
            "kind": "entry_fill",
            "trade_id": trade["id"],
            "trade_status": "CONFIRMED",
            "transaction_ref": trade["transaction_hash"],
            "gross_filled_shares": trade["size"],
            "net_position_delta_shares": trade["size"],
            "trader_side": trade["trader_side"],
            "fee_rate_bps": trade["fee_rate_bps"],
            "maker_address": trade["maker_address"],
            "match_time": trade["match_time"],
            "last_update": trade["last_update"],
            "evidence_source": "authenticated_clob_trade",
        }
    )
    return result


def adapt_account_snapshot(
    *,
    order: dict[str, Any] | None,
    trades: Iterable[dict[str, Any]],
    expected: dict[str, Any],
    required_trader_side: str | None = None,
) -> dict[str, Any]:
    """Adapt an authenticated order plus account trades deterministically.

    Duplicate trade rows are collapsed by event id.  Public Data API rows are
    rejected naturally because they lack the authenticated ``owner`` field and
    the CLOB order/trade fields required for account reconciliation.
    """
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    order_result = adapt_order(order, expected=expected) if order is not None else {"events": [], "issues": [], "order": None}
    events.extend(order_result["events"])
    issues.extend(order_result["issues"])
    observed_order_id = _text((order_result.get("order") or {}).get("id")) or _text(_pick(expected, "order_id"))
    seen_events: set[str] = {str(event["event_id"]) for event in events}
    seen_issue_keys: set[str] = set()
    for issue in issues:
        seen_issue_keys.add(json_key(issue))
    for payload in trades:
        trade_result = adapt_trade(
            payload,
            expected=expected,
            order_id=observed_order_id or None,
            required_trader_side=required_trader_side,
        )
        for issue in trade_result["issues"]:
            key = json_key(issue)
            if key not in seen_issue_keys:
                issues.append(issue)
                seen_issue_keys.add(key)
        for event in trade_result["events"]:
            event_id = str(event["event_id"])
            if event_id not in seen_events:
                events.append(event)
                seen_events.add(event_id)
    return {"events": events, "issues": issues, "order": order_result.get("order")}


def json_key(value: dict[str, Any]) -> str:
    """Stable issue key without importing a serializer into the adapter path."""
    return "|".join(f"{key}={value[key]}" for key in sorted(value))

