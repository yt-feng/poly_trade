"""Pure correlation of a public wallet activity row with an account trade.

Wallet activity can corroborate the transaction hash, market, token, side and
share quantity of an already authenticated CLOB trade.  It cannot create a
fill, prove account ownership by itself, or release cash.  No network, wallet,
signing, balance, or order capability exists here.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


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
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _canonical_activity(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "activity_id": _text(_pick(payload, "id", "activityId", "activity_id")),
        "activity_type": _text(_pick(payload, "type", "eventType", "event_type")).upper(),
        "wallet": _text(_pick(payload, "proxyWallet", "proxy_wallet", "wallet", "owner")),
        "market_id": _text(_pick(payload, "conditionId", "condition_id", "market", "market_id")),
        "token_id": _text(_pick(payload, "asset", "assetId", "asset_id", "tokenId", "token_id")),
        "side": _text(_pick(payload, "side")).upper(),
        "transaction_hash": _text(_pick(payload, "transactionHash", "transaction_hash", "tx_hash")),
        "timestamp": _text(_pick(payload, "timestamp", "createdAt", "created_at")),
        "size": _text(_pick(payload, "size", "shares")),
        "usdc_size": _text(_pick(payload, "usdcSize", "usdc_size", "amount")),
        "price": _text(_pick(payload, "price")),
    }


def correlate_activity(
    payload: dict[str, Any], *, trade: dict[str, Any], expected: dict[str, Any]
) -> dict[str, Any]:
    """Return a corroboration result without turning activity into cash evidence."""
    result: dict[str, Any] = {
        "matched": False,
        "issues": [],
        "activity": None,
        "cash_reusable": False,
        "evidence_source": "public_wallet_activity_corroboration",
    }
    if not isinstance(payload, dict):
        result["issues"].append(_issue("INVALID_ACTIVITY_PAYLOAD", "activity must be an object"))
        return result
    activity = _canonical_activity(payload)
    result["activity"] = activity
    issues: list[dict[str, Any]] = []
    if activity["activity_type"] != "TRADE":
        issues.append(_issue("UNSUPPORTED_ACTIVITY_TYPE", "only TRADE activity can corroborate a trade", activity_type=activity["activity_type"]))
    owner = _text(expected.get("owner") or trade.get("owner"))
    if not owner or not activity["wallet"]:
        issues.append(_issue("MISSING_OWNER_SCOPE", "activity corroboration requires wallet and expected owner"))
    elif activity["wallet"].casefold() != owner.casefold():
        issues.append(_issue("OWNER_MISMATCH", "activity wallet does not match the authenticated owner"))
    for field, activity_key, trade_key in (
        ("market_id", "market_id", "market_id"),
        ("token_id", "token_id", "asset_id"),
        ("side", "side", "side"),
    ):
        expected_value = _text(trade.get(trade_key) or expected.get(field))
        if not expected_value or activity[activity_key] != expected_value:
            issues.append(_issue("TRADE_ACTIVITY_IDENTITY_MISMATCH", f"activity {field} does not match the trade", field=field, expected=expected_value, actual=activity[activity_key]))
    tx = _text(trade.get("transaction_hash") or trade.get("transaction_ref"))
    if not tx or not activity["transaction_hash"]:
        issues.append(_issue("MISSING_ACTIVITY_TRANSACTION_HASH", "activity corroboration requires the trade transaction hash"))
    elif activity["transaction_hash"].casefold() != tx.casefold():
        issues.append(_issue("TRANSACTION_HASH_MISMATCH", "activity transaction hash does not match the trade"))
    if not activity["timestamp"]:
        issues.append(_issue("MISSING_ACTIVITY_TIMESTAMP", "activity corroboration requires a timestamp"))
    try:
        activity_size = _decimal(activity["size"], "activity size")
        trade_size = _decimal(trade.get("size") or trade.get("gross_filled_shares"), "trade size")
        if activity_size <= 0 or trade_size <= 0:
            issues.append(_issue("INVALID_ACTIVITY_QUANTITY", "activity and trade quantities must be positive"))
        elif abs(activity_size - trade_size) > max(Decimal("1e-8"), abs(trade_size) * Decimal("1e-8")):
            issues.append(_issue("ACTIVITY_QUANTITY_MISMATCH", "activity share quantity does not match the trade", activity_size=str(activity_size), trade_size=str(trade_size)))
    except ValueError as exc:
        issues.append(_issue("INVALID_ACTIVITY_QUANTITY", str(exc)))
    result["issues"] = issues
    if issues:
        return result
    activity_id = activity["activity_id"] or ":".join(
        (activity["transaction_hash"], activity["market_id"], activity["token_id"], activity["timestamp"])
    )
    result["matched"] = True
    result["observation"] = {
        "observation_id": f"activity:{activity_id}:trade_corroboration",
        "trade_id": _text(trade.get("id") or trade.get("trade_id")),
        "transaction_ref": activity["transaction_hash"],
        "activity_id": activity_id,
        "evidence_source": "public_wallet_activity_corroboration",
        "cash_reusable": False,
    }
    return result
