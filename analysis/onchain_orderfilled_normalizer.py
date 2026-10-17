"""Pure normalizer for decoded Polymarket OrderFilled records.

The adapter accepts records already decoded from a receipt or an archival
scrape.  It does not call an RPC, inspect a wallet, or submit an order.  V1
uses maker/taker asset IDs to recover the aggressor side; V2 carries an
explicit ``side`` and a single ``token_id``.  Reverted records are rejected
and the event identity is stable at ``(tx_hash, log_index)`` for idempotent
replay.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

USDC_ASSET_ID = "0"
USDC_SCALE = Decimal(1_000_000)
V1_ORDERFILLED_FIELDS = (
    "order_hash", "maker", "taker", "maker_asset_id", "taker_asset_id",
    "maker_amount_filled", "taker_amount_filled", "fee",
)
V2_ORDERFILLED_FIELDS = (
    "order_hash", "maker", "taker", "side", "token_id",
    "maker_amount_filled", "taker_amount_filled", "fee", "builder", "metadata",
)


def _text(value: Any, field: str) -> str:
    out = "" if value is None else str(value).strip()
    if not out:
        raise ValueError(f"{field} is required")
    return out


def _integer(value: Any, field: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        out = int(str(value), 0) if isinstance(value, str) and str(value).lower().startswith("0x") else int(value)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if out < 0:
        raise ValueError(f"{field} must be nonnegative")
    return out


def _decimal(value: Any, field: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not out.is_finite() or out < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return out


def _asset_id(value: Any, field: str) -> str:
    """Canonicalize decimal or hex uint256 asset IDs without float conversion."""
    try:
        if isinstance(value, bool):
            raise ValueError
        out = int(str(value), 0) if isinstance(value, str) and str(value).lower().startswith("0x") else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an asset id") from exc
    if out < 0:
        raise ValueError(f"{field} must be nonnegative")
    return str(out)


def _reverted(record: dict[str, Any]) -> bool:
    if bool(record.get("reverted")) or bool(record.get("transaction_reverted")):
        return True
    for key in ("tx_status", "transaction_status", "receipt_status", "status"):
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if isinstance(value, bool):
            return not value
        text = str(value).strip().lower()
        if text in {"0", "0x0", "false", "reverted", "failed"}:
            return True
        if text in {"1", "0x1", "true", "success", "succeeded", "mined", "confirmed"}:
            return False
    return False


def _base(record: dict[str, Any], version: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if version not in {"v1", "v2"}:
        raise ValueError("exchange_version must be v1 or v2")
    if _reverted(record):
        raise ValueError("reverted_transaction")
    tx_hash = _text(record.get("tx_hash", record.get("transaction_hash")), "tx_hash")
    log_index = _integer(record.get("log_index"), "log_index")
    block_number = _integer(record.get("block_number"), "block_number")
    return {
        "event_id": f"{tx_hash}:{log_index}",
        "exchange_version": version,
        "block_number": block_number,
        "tx_hash": tx_hash,
        "log_index": log_index,
        "order_hash": _text(record.get("order_hash"), "order_hash"),
        "maker": _text(record.get("maker"), "maker").lower(),
        "taker": _text(record.get("taker"), "taker").lower(),
        "fee_raw": str(_integer(record.get("fee", 0), "fee")),
        "block_timestamp": record.get("block_timestamp", record.get("timestamp")),
        "market_id": record.get("market_id", record.get("condition_id")),
        "timestamp_ms": record.get("timestamp_ms", record.get("block_timestamp_ms")),
    }


def normalize_orderfilled(record: dict[str, Any], *, exchange_version: str) -> dict[str, Any]:
    """Return one canonical fill or raise a fail-closed validation error."""
    out = _base(record, exchange_version)
    maker_amount = _decimal(record.get("maker_amount_filled"), "maker_amount_filled")
    taker_amount = _decimal(record.get("taker_amount_filled"), "taker_amount_filled")
    if maker_amount <= 0 or taker_amount <= 0:
        raise ValueError("fill amounts must be positive")

    if exchange_version == "v1":
        maker_asset = _asset_id(record.get("maker_asset_id"), "maker_asset_id")
        taker_asset = _asset_id(record.get("taker_asset_id"), "taker_asset_id")
        if taker_asset == USDC_ASSET_ID and maker_asset != USDC_ASSET_ID:
            side, token_id = "BUY", maker_asset
            size_raw, cash_raw = maker_amount, taker_amount
            sign = 1
        elif maker_asset == USDC_ASSET_ID and taker_asset != USDC_ASSET_ID:
            side, token_id = "SELL", taker_asset
            size_raw, cash_raw = taker_amount, maker_amount
            sign = -1
        else:
            raise ValueError("unsupported_split_or_collateral_side")
    else:
        side_value = record.get("side")
        if isinstance(side_value, str):
            side_text = side_value.strip().upper()
            if side_text in {"0", "BUY"}:
                side = "BUY"
            elif side_text in {"1", "SELL"}:
                side = "SELL"
            else:
                raise ValueError("unsupported_v2_side")
        else:
            side = {0: "BUY", 1: "SELL"}.get(_integer(side_value, "side"))
            if side is None:
                raise ValueError("unsupported_v2_side")
        token_id = _asset_id(record.get("token_id"), "token_id")
        if side == "BUY":
            size_raw, cash_raw, sign = maker_amount, taker_amount, 1
        else:
            size_raw, cash_raw, sign = taker_amount, maker_amount, -1

    size = size_raw / USDC_SCALE
    cash = cash_raw / USDC_SCALE
    if size <= 0 or cash <= 0:
        raise ValueError("normalized size and cash must be positive")
    price = cash / size
    if not (Decimal("0") < price < Decimal("1")):
        raise ValueError("normalized price must be between zero and one")
    out.update({
        "token_id": token_id,
        "side": side,
        "sign": sign,
        "size": str(size),
        "cash_amount": str(cash),
        "price": str(price),
    })
    return out


def deduplicate_orderfilled(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and deduplicate canonical records by transaction/log identity."""
    seen: dict[str, dict[str, Any]] = {}
    for item in records:
        event_id = _text(item.get("event_id"), "event_id")
        prior = seen.get(event_id)
        if prior is not None and prior != item:
            raise ValueError("conflicting_duplicate_event")
        seen[event_id] = item
    return [seen[key] for key in sorted(seen)]


def quote_join_ready(fill: dict[str, Any], quote: dict[str, Any], *, tolerance_ms: int = 2500) -> bool:
    """Check the minimum causal keys for joining one fill to a quote row."""
    if tolerance_ms < 0:
        raise ValueError("tolerance_ms must be nonnegative")
    if not fill.get("token_id") or not quote.get("token_id"):
        return False
    if str(fill["token_id"]) != str(quote["token_id"]):
        return False
    if fill.get("market_id") and quote.get("market_id") and str(fill["market_id"]) != str(quote["market_id"]):
        return False
    try:
        return abs(int(fill["timestamp_ms"]) - int(quote["timestamp_ms"])) <= tolerance_ms
    except (KeyError, TypeError, ValueError):
        return False
