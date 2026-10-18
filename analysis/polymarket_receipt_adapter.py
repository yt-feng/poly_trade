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


def _text_list(value: Any, field: str) -> list[str]:
    """Normalize an optional list of opaque receipt identifiers."""
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return [_text(item) for item in value if _text(item)]


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
    transaction_hashes = _pick(
        payload, "transaction_hashes", "transactionsHashes", default=[]
    )
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
        "trade_ids": _text_list(trade_ids, "trade_ids"),
        # The official client may return transaction hashes before the trade
        # record has resolved. Preserve them as order evidence, but never use
        # them alone to emit an entry_fill event.
        "transaction_hashes": _text_list(transaction_hashes, "transaction_hashes"),
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
        # Preserve the executed price as authenticated trade evidence.  It is
        # not used to promote a fill; status, timestamps and transaction hash
        # remain required below.
        "price": _text(_pick(payload, "price")),
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
        # Preserve the documented epoch timestamp even when the optional
        # matchedAt/updatedAt fields are absent; it is evidence metadata, not
        # a substitute for the stricter confirmation timestamps above.
        "timestamp": _text(_pick(payload, "timestamp")),
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
            "transaction_hashes": list(order["transaction_hashes"]),
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
    if not trade["price"]:
        issues.append(
            _issue(
                "MISSING_TRADE_PRICE",
                "a confirmed fill must include its executed price",
            )
        )
    else:
        try:
            price = _decimal(trade["price"], "price")
            if price <= 0 or price >= 1:
                issues.append(
                    _issue(
                        "INVALID_TRADE_PRICE",
                        "trade price must be strictly between zero and one",
                        price=trade["price"],
                    )
                )
        except ValueError as exc:
            issues.append(_issue("INVALID_TRADE_PRICE", str(exc)))
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
            "owner": trade["owner"],
            "market_id": trade["market_id"],
            "asset_id": trade["asset_id"],
            "taker_order_id": trade["taker_order_id"],
            "maker_order_ids": sorted(_maker_order_ids(trade["maker_orders"])),
            "trade_owner": trade["trade_owner"],
            "side": trade["side"],
            "price": trade["price"],
            "size": trade["size"],
            # v2 compound admission names the same executed share quantity
            # ``shares`` and the role ``execution_role``.  Preserve the
            # legacy receipt names above while emitting explicit aliases so a
            # confirmed account event cannot silently lose the fields needed
            # by the one-ledger validator.  Fee-rate conversion remains
            # deliberately separate because the raw API field is named bps
            # while the frozen v2 schedule is a decimal rate.
            "shares": trade["size"],
            "execution_role": trade["trader_side"],
            "transaction_ref": trade["transaction_hash"],
            "gross_filled_shares": trade["size"],
            "net_position_delta_shares": trade["size"],
            "trader_side": trade["trader_side"],
            "fee_rate_bps": trade["fee_rate_bps"],
            "maker_address": trade["maker_address"],
            "match_time": trade["match_time"],
            "last_update": trade["last_update"],
            "timestamp": trade["timestamp"],
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

# Strict read-only user-stream normalization retained from the default branch.
_ORDER_TERMINAL = frozenset({"CANCELED", "UNMATCHED"})
_ORDER_EVENT_TYPES = frozenset({"PLACEMENT", "UPDATE", "CANCELLATION"})
_TRADE_PENDING = frozenset({
    "MATCHED", "MATCHED_NOT_BROADCASTED", "MINED", "RETRYING",
    "TRADE_STATUS_MATCHED", "TRADE_STATUS_MATCHED_NOT_BROADCASTED",
    "TRADE_STATUS_MINED", "TRADE_STATUS_RETRYING",
})
_TRADE_CONFIRMED = frozenset({"CONFIRMED", "TRADE_STATUS_CONFIRMED"})
_TRADE_FAILED = frozenset({"FAILED", "TRADE_STATUS_FAILED"})


def _value(obj: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _decimal_text(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not out.is_finite() or out < 0:
        raise ValueError(f"invalid {field}")
    return format(out, "f")


def _payload(event: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(event, dict):
        raise ValueError("user event must be an object")
    payload = event.get("payload")
    body = payload if isinstance(payload, dict) else event
    # The wire form uses ``event_type=order`` with ``type=PLACEMENT``;
    # nested SDK events use an outer ``type=order``.  Prefer event_type so a
    # lifecycle verb is never mistaken for the event family.
    kind = str(_value(event, "event_type") or "").strip().lower()
    if not kind:
        candidate = str(_value(event, "type") or body.get("type") or "").strip().lower()
        if candidate in {"order", "trade", "order_update", "trade_update"}:
            kind = candidate
    if kind in {"order", "order_update"}:
        return "order", body
    if kind in {"trade", "trade_update"}:
        return "trade", body
    raise ValueError("unsupported user event type")


def normalize_user_event(event: Any) -> dict[str, Any]:
    """Normalize one official user-stream order/trade event.

    ``sizeMatched`` is retained as ``matched_shares`` only.  It never becomes
    ``confirmed_filled_shares`` unless a trade event reaches a confirmed state.
    Cash reconciliation is deliberately absent and must be supplied separately.
    """
    kind, body = _payload(event)
    order_id = _value(body, "id", "order_id")
    submission_id = _value(body, "takerOrderId", "taker_order_id", "order_id") if kind == "trade" else order_id
    condition_id = _value(body, "conditionId", "condition_id", "market")
    token_id = _value(body, "assetId", "asset_id")
    if not all(isinstance(x, str) and x.strip() for x in (submission_id, condition_id, token_id)):
        raise ValueError("order/trade identity incomplete")

    out: dict[str, Any] = {
        "submission_reference": submission_id.strip(),
        "condition_id": condition_id.strip(),
        "token_id": token_id.strip(),
        "event_kind": kind,
    }
    if kind == "order":
        status = str(_value(body, "status") or "").strip().upper()
        lifecycle = str(_value(body, "orderEventType", "order_event_type") or "").strip().upper()
        # Explicit SDK aliases win, including empty/unknown supplied values.
        # Only lifecycle verbs may fall back from wire type, never SDK families.
        if "orderEventType" not in body and "order_event_type" not in body:
            wire_type = str(body.get("type") or "").strip().upper()
            if wire_type in _ORDER_EVENT_TYPES:
                lifecycle = wire_type
        requested = _decimal_text(_value(body, "originalSize", "original_size"), "original size")
        matched = _decimal_text(_value(body, "sizeMatched", "size_matched"), "matched size")
        if requested is not None:
            out["requested_shares"] = requested
        if matched is not None:
            out["matched_shares"] = matched
        out.update({
            "order_status": status,
            "order_event_type": lifecycle,
            "order_terminal": status in _ORDER_TERMINAL,
            "entry_order_terminal": status in _ORDER_TERMINAL,
            "confirmed_filled_shares": "0",
            "private_fill_inferred": False,
        })
        return out

    status = str(_value(body, "status", "trade_status") or "").strip().upper()
    matched = _decimal_text(_value(body, "size", "matchedAmount", "matched_amount"), "trade size")
    if matched is not None:
        out["matched_shares"] = matched
    out.update({
        "trade_status": status,
        "transaction_ref": _value(body, "transactionHash", "transaction_hash"),
        "trade_id": _value(body, "id", "trade_id"),
        "confirmed_filled_shares": matched if status in _TRADE_CONFIRMED else "0",
        "trade_confirmed": status in _TRADE_CONFIRMED,
        "trade_failed": status in _TRADE_FAILED,
        "trade_pending": status in _TRADE_PENDING,
        "private_fill_inferred": False,
    })
    return out
