"""Strict, read-only normalization of Polymarket user-stream wire events.

The adapter intentionally stops at supplied evidence.  It does not authenticate
an account, place or cancel orders, release cash, or turn a matched event into a
confirmed fill.  The normalized fields are shaped for ``execution_reason_codes``
and the cash/receipt audits.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


_ORDER_TERMINAL = frozenset({"CANCELED", "UNMATCHED"})
_TRADE_PENDING = frozenset({
    "MATCHED", "MATCHED_NOT_BROADCASTED", "MINED", "RETRYING",
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
        requested = _decimal_text(_value(body, "originalSize", "original_size"), "original size")
        matched = _decimal_text(_value(body, "sizeMatched", "size_matched"), "matched size")
        if requested is not None:
            out["requested_shares"] = requested
        if matched is not None:
            out["matched_shares"] = matched
        out.update({
            "order_status": status,
            "order_event_type": str(_value(body, "orderEventType", "order_event_type") or "").strip().upper(),
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
