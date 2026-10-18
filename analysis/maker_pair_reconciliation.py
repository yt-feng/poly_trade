"""Read-only reconciliation for a complementary maker pair.

This module consumes authenticated trade receipts supplied by a caller.  It
does not connect to a venue, infer a fill from quotes, merge positions, or
release cash.  A pair is only a settlement candidate until an independent
balance/merge checkpoint is supplied.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


def _dec(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def reconcile_maker_pair(
    trades: Iterable[dict[str, Any]],
    *,
    expected_owner: str,
    expected_market_id: str,
    up_asset_id: str,
    down_asset_id: str,
    minimum_leg_notional: Any = "5",
    maximum_match_age_seconds: Any = "30",
) -> dict[str, Any]:
    """Validate two complementary maker trade receipts without releasing cash."""
    rows = list(trades)
    issues: list[str] = []
    if len(rows) != 2:
        issues.append("PAIR_REQUIRES_EXACTLY_TWO_TRADES")
    if not expected_owner or not expected_market_id or not up_asset_id or not down_asset_id:
        issues.append("PAIR_IDENTITY_REQUIRED")
    try:
        min_leg = _dec(minimum_leg_notional, "minimum_leg_notional")
        max_age = _dec(maximum_match_age_seconds, "maximum_match_age_seconds")
        if min_leg <= 0 or max_age < 0:
            issues.append("PAIR_PROTOCOL_BOUNDS_INVALID")
    except ValueError as exc:
        issues.append(str(exc))
        min_leg = Decimal("0")
        max_age = Decimal("0")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("PAIR_TRADE_NOT_OBJECT")
            continue
        try:
            trade_id = _text(row.get("id"))
            market_id = _text(row.get("market", row.get("conditionId", row.get("condition_id"))))
            asset_id = _text(row.get("asset_id", row.get("assetId", row.get("token_id"))))
            owner = _text(row.get("owner"))
            status = _text(row.get("status")).upper()
            trader_side = _text(row.get("trader_side", row.get("traderSide"))).upper()
            fee_rate = _dec(row.get("fee_rate_bps", row.get("feeRateBps")), "fee_rate_bps")
            size = _dec(row.get("size"), "size")
            price = _dec(row.get("price"), "price")
            match_time = _dec(row.get("match_time", row.get("matchedAt", row.get("matched_at"))), "match_time")
            transaction_hash = _text(row.get("transaction_hash", row.get("transactionHash")))
        except ValueError as exc:
            issues.append(str(exc))
            continue
        if not trade_id:
            issues.append("PAIR_TRADE_ID_REQUIRED")
        if market_id != expected_market_id:
            issues.append("PAIR_MARKET_MISMATCH")
        if owner != expected_owner:
            issues.append("PAIR_OWNER_MISMATCH")
        if status != "TRADE_STATUS_CONFIRMED":
            issues.append("PAIR_TRADE_NOT_CONFIRMED")
        if trader_side != "MAKER":
            issues.append("PAIR_TRADE_NOT_MAKER")
        if fee_rate != 0:
            issues.append("PAIR_MAKER_FEE_NOT_ZERO")
        if size <= 0 or price <= 0 or price >= 1:
            issues.append("PAIR_TRADE_QUANTITY_OR_PRICE_INVALID")
        if not transaction_hash:
            issues.append("PAIR_TRANSACTION_HASH_REQUIRED")
        normalized.append({
            "id": trade_id, "market_id": market_id, "asset_id": asset_id,
            "owner": owner, "status": status, "trader_side": trader_side,
            "fee_rate_bps": str(fee_rate), "size": size, "price": price,
            "match_time": match_time, "transaction_hash": transaction_hash,
        })

    if len({row["id"] for row in normalized if row["id"]}) != len([row for row in normalized if row["id"]]):
        issues.append("PAIR_DUPLICATE_TRADE_ID")
    assets = {row["asset_id"] for row in normalized}
    if assets != {up_asset_id, down_asset_id}:
        issues.append("PAIR_ASSET_COMPLEMENT_MISMATCH")
    if normalized:
        times = [row["match_time"] for row in normalized]
        if max(times) - min(times) > max_age:
            issues.append("PAIR_MATCHES_TOO_FAR_APART")

    result: dict[str, Any] = {
        "issues": sorted(set(issues)),
        "pair_status": "rejected",
        "pair_trade_ids": sorted(row["id"] for row in normalized if row["id"]),
        "matched_shares": "0",
        "gross_cost": "0",
        "fee": "0",
        "pair_edge": "0",
        "cash_reusable": False,
        "settlement_checkpoint_required": True,
        "evidence_source": "authenticated_clob_trade_pair",
    }
    if issues or len(normalized) != 2:
        return result

    up, down = (next(row for row in normalized if row["asset_id"] == asset) for asset in (up_asset_id, down_asset_id))
    matched = min(up["size"], down["size"])
    leg_notional_up = matched * up["price"]
    leg_notional_down = matched * down["price"]
    if leg_notional_up < min_leg or leg_notional_down < min_leg:
        result["issues"] = ["PAIR_LEG_BELOW_MINIMUM_NOTIONAL"]
        return result
    gross_cost = leg_notional_up + leg_notional_down
    edge = matched - gross_cost
    result.update({
        "pair_status": "settlement_candidate" if edge > 0 else "rejected",
        "matched_shares": str(matched),
        "gross_cost": str(gross_cost),
        "pair_edge": str(edge),
        "pair_prices": {"up": str(up["price"]), "down": str(down["price"])},
    })
    if edge <= 0:
        result["issues"] = ["PAIR_NON_POSITIVE_EDGE"]
    return result
