"""Pure adapter for already captured Polymarket authenticated user frames.

The real-time user stream carries an envelope with ``topic=user``, a
``type`` of ``order`` or ``trade``, and a payload.  This module only validates
and translates one supplied frame into the existing receipt adapter events;
it never opens a socket, authenticates, signs, submits, cancels, sells,
redeems, or reads a balance.  That keeps a captured frame useful for
reconciliation without turning the research checkout into a trading client.
"""
from __future__ import annotations

from typing import Any, Iterable

from polymarket_receipt_adapter import adapt_order, adapt_trade, json_key


USER_TOPIC = "user"
USER_EVENT_TYPES = frozenset({"order", "trade"})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _empty_result() -> dict[str, Any]:
    return {
        "events": [],
        "issues": [],
        "raw_event_type": "",
        "dedupe_key": None,
        "duplicate": False,
        "payload": None,
    }


def _canonical_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Translate the documented camelCase wire shape to receipt-adapter keys."""
    if event_type == "order":
        return {
            "id": payload.get("id"),
            "owner": payload.get("owner"),
            "conditionId": payload.get("conditionId", payload.get("condition_id")),
            "assetId": payload.get("assetId", payload.get("asset_id")),
            "side": payload.get("side"),
            "originalSize": payload.get("originalSize", payload.get("original_size")),
            "sizeMatched": payload.get("sizeMatched", payload.get("size_matched")),
            "associateTrades": payload.get(
                "associateTrades", payload.get("associate_trades", [])
            ),
            "tradeIDs": payload.get("tradeIDs", payload.get("trade_ids", [])),
            "transactionsHashes": payload.get(
                "transactionsHashes", payload.get("transaction_hashes", [])
            ),
            "status": payload.get("status"),
        }
    return {
        "id": payload.get("id"),
        "takerOrderId": payload.get("takerOrderId", payload.get("taker_order_id")),
        "owner": payload.get("owner"),
        "tradeOwner": payload.get("tradeOwner", payload.get("trade_owner")),
        "conditionId": payload.get("conditionId", payload.get("condition_id")),
        "assetId": payload.get("assetId", payload.get("asset_id")),
        "side": payload.get("side"),
        "size": payload.get("size"),
        "price": payload.get("price"),
        "status": payload.get("status"),
        "feeRateBps": payload.get("feeRateBps", payload.get("fee_rate_bps")),
        "makerAddress": payload.get("makerAddress", payload.get("maker_address")),
        "transactionHash": payload.get(
            "transactionHash", payload.get("transaction_hash")
        ),
        "matchedAt": payload.get("matchedAt", payload.get("match_time")),
        "updatedAt": payload.get("updatedAt", payload.get("last_update")),
        "traderSide": payload.get("traderSide", payload.get("trader_side")),
        "makerOrders": payload.get("makerOrders", payload.get("maker_orders", [])),
        "timestamp": payload.get("timestamp"),
    }


def _dedupe_key(event_type: str, payload: dict[str, Any]) -> str:
    status = _text(
        payload.get("status")
        or payload.get("orderEventType")
        or payload.get("order_event_type")
    ).upper()
    revision = _text(
        payload.get("updatedAt")
        or payload.get("updated_at")
        or payload.get("last_update")
        or payload.get("timestamp")
    )
    return f"{event_type}:{_text(payload.get('id'))}:{status}:{revision}"


def adapt_user_stream_frame(
    frame: dict[str, Any],
    *,
    expected: dict[str, Any],
    required_trader_side: str | None = None,
    seen_event_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Adapt one captured authenticated user-stream frame.

    ``seen_event_ids`` is an optional caller-owned set.  A valid frame's
    ``type:id:status:revision`` key is added to that set before adaptation, so
    retrying the same delivery cannot append a second receipt event while a
    later status update for the same trade (for example MATCHED -> CONFIRMED)
    remains eligible for validation.  The function is deterministic and does
    not mutate the payload.
    """
    result = _empty_result()
    if not isinstance(frame, dict):
        result["issues"].append(
            _issue("INVALID_USER_FRAME", "user stream frame must be an object")
        )
        return result

    event_type = _text(frame.get("type")).lower()
    result["raw_event_type"] = event_type
    if _text(frame.get("topic")).lower() != USER_TOPIC:
        result["issues"].append(
            _issue(
                "UNAUTHENTICATED_USER_TOPIC",
                "only a frame explicitly tagged with topic=user is an account receipt",
                topic=_text(frame.get("topic")),
            )
        )
        return result
    if event_type not in USER_EVENT_TYPES:
        result["issues"].append(
            _issue(
                "UNKNOWN_USER_EVENT_TYPE",
                "user stream type must be order or trade",
                event_type=event_type,
            )
        )
        return result

    payload = frame.get("payload")
    if not isinstance(payload, dict):
        result["issues"].append(
            _issue("INVALID_USER_PAYLOAD", "user stream payload must be an object")
        )
        return result
    result["payload"] = dict(payload)
    identifier = _text(payload.get("id"))
    if not identifier:
        result["issues"].append(
            _issue("MISSING_USER_EVENT_ID", "authenticated user event requires payload.id")
        )
        return result

    dedupe_key = _dedupe_key(event_type, payload)
    result["dedupe_key"] = dedupe_key
    if seen_event_ids is not None:
        if dedupe_key in seen_event_ids:
            result["duplicate"] = True
            return result
        seen_event_ids.add(dedupe_key)

    canonical = _canonical_payload(event_type, payload)
    if event_type == "order":
        adapted = adapt_order(canonical, expected=expected)
    else:
        adapted = adapt_trade(
            canonical,
            expected=expected,
            required_trader_side=required_trader_side,
        )
    result["events"] = list(adapted["events"])
    result["issues"] = list(adapted["issues"])
    # Keep the stream metadata available to a journal without changing the
    # receipt-state event contract.
    result["stream_timestamp"] = payload.get("timestamp")
    if event_type == "order":
        result["order"] = adapted.get("order")
    else:
        result["trade"] = adapted.get("trade")
    return result


def adapt_user_stream_frames(
    frames: Iterable[dict[str, Any]],
    *,
    expected: dict[str, Any],
    required_trader_side: str | None = None,
) -> dict[str, Any]:
    """Adapt a finite captured stream slice, preserving order and idempotency."""
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    frames_seen = 0
    duplicates = 0
    for frame in frames:
        frames_seen += 1
        result = adapt_user_stream_frame(
            frame,
            expected=expected,
            required_trader_side=required_trader_side,
            seen_event_ids=seen_event_ids,
        )
        if result["duplicate"]:
            duplicates += 1
        for event in result["events"]:
            event_id = str(event["event_id"])
            if event_id not in {str(existing["event_id"]) for existing in events}:
                events.append(event)
        seen_issue_keys = {json_key(issue) for issue in issues}
        for issue in result["issues"]:
            key = json_key(issue)
            if key not in seen_issue_keys:
                issues.append(issue)
                seen_issue_keys.add(key)
    return {
        "events": events,
        "issues": issues,
        "frames_seen": frames_seen,
        "duplicate_frames": duplicates,
        "seen_event_ids": sorted(seen_event_ids),
    }

