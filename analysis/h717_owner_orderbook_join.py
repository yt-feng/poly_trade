"""H717: read-only owner-trade to public L2 order-book alignment.

This is an evidence-boundary audit, not a strategy replay.  It joins the
already authenticated trade rows captured by H279 with PMXT v2 ``book``
snapshots fetched in the cloud.  The output is intentionally private: market
and transaction identifiers stay inside the encrypted research vault.

The join never signs, submits, cancels, redeems, merges, transfers, or reads
an account balance.  A matching book row is not treated as proof that the
owner's fill was caused by that displayed quote; it only measures whether a
contemporaneous public snapshot is available and whether the displayed
ladder could cover the observed size under an explicitly conservative rule.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


FORMAT = "h717-owner-orderbook-join-v1"
PMXT_V2_BASE = "https://r2v2.pmxt.dev"
BTC5M_RE = re.compile(r"(?:^|[-_])(btc|bitcoin)[-_](?:updown|up_down)[-_]5m(?:[-_]|$)", re.I)
MAX_TRADES = 200
MAX_HOURS = 96
MAX_ROWS_PER_HOUR = 250_000
MAX_SNAPSHOT_AGE_US = 10_000_000
MAX_EVENT_LAG_US = 120_000_000


class JoinError(ValueError):
    """Raised for malformed or unsafe evidence input."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _timestamp_us(value: Any) -> int | None:
    """Normalize ISO, seconds, milliseconds, or microseconds to UTC us."""
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        if abs(number) < 10**11:
            return int(number * 1_000_000)
        if abs(number) < 10**14:
            return int(number * 1_000)
        return int(number)
    text = _text(value)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None and math.isfinite(number):
        if abs(number) < 10**11:
            return int(number * 1_000_000)
        if abs(number) < 10**14:
            return int(number * 1_000)
        return int(number)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000)


def _iso_hour(ts_us: int) -> str:
    return datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H")


def _lower(value: Any) -> str:
    return _text(value).lower()


def _levels(value: Any) -> list[tuple[float, float]]:
    """Parse PMXT JSON book levels, accepting [price,size] or object rows."""
    if isinstance(value, str):
        if not value:
            return []
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    result: list[tuple[float, float]] = []
    for item in value:
        price = size = None
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = item[0], item[1]
        elif isinstance(item, dict):
            price, size = item.get("price"), item.get("size")
        price_n, size_n = _finite(price), _finite(size)
        if price_n is None or size_n is None or not (0 < price_n < 1) or size_n < 0:
            continue
        result.append((price_n, size_n))
    return result


def _best(levels: list[tuple[float, float]], *, asks: bool) -> tuple[float, float] | None:
    if not levels:
        return None
    return (min if asks else max)(levels, key=lambda row: row[0])


def _ladder_cover(levels: list[tuple[float, float]], *, asks: bool, trade_price: float, trade_size: float) -> dict[str, Any]:
    """Measure touch/ladder coverage without pretending to be an execution."""
    ordered = sorted(levels, key=lambda row: row[0], reverse=not asks)
    consumed = 0.0
    eligible = []
    for price, size in ordered:
        # A buy can consume asks up to its observed trade price; a sell can
        # consume bids down to that price.  Never count levels beyond the
        # observed execution price.
        if asks and price - 1e-12 > trade_price:
            continue
        if not asks and price + 1e-12 < trade_price:
            continue
        eligible.append((price, size))
        consumed += size
        if consumed + 1e-12 >= trade_size:
            break
    touch = _best(levels, asks=asks)
    touch_price = touch[0] if touch else None
    touch_size = touch[1] if touch else None
    return {
        "touch_price": touch_price,
        "touch_size": touch_size,
        "ladder_depth_to_trade_price": consumed,
        "displayed_size_covers_trade": bool(trade_size >= 0 and consumed + 1e-12 >= trade_size and eligible),
        "trade_price_touches_displayed_ladder": bool(
            eligible and ((asks and eligible[0][0] <= trade_price + 1e-12) or (not asks and eligible[0][0] >= trade_price - 1e-12))
        ),
    }


def _activity_slug_map(account: Mapping[str, Any]) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for item in account.get("public_data_activity") or account.get("public_activity") or []:
        if not isinstance(item, dict):
            continue
        slug = _lower(item.get("slug") or item.get("eventSlug"))
        if not slug or not BTC5M_RE.search(slug):
            continue
        tx = _lower(item.get("transactionHash") or item.get("transaction_hash"))
        condition = _lower(item.get("conditionId") or item.get("condition_id"))
        asset = _lower(item.get("asset") or item.get("asset_id") or item.get("token_id"))
        if tx or condition or asset:
            result[(tx, condition, asset)] = slug
    return result


def normalize_trades(h279: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract explicit BTC 5m trades, retaining owner scope only as a digest."""
    accounts = h279.get("accounts")
    if not isinstance(accounts, list):
        raise JoinError("h279_accounts_missing")
    output: list[dict[str, Any]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        label = _text(account.get("label"))
        owner_digest = _text(account.get("owner_address_sha256")) or _digest(_text(account.get("label")))
        slug_map = _activity_slug_map(account)
        for raw in account.get("account_trades") or []:
            if not isinstance(raw, dict):
                continue
            tx = _lower(raw.get("transaction_hash") or raw.get("transactionHash"))
            condition = _lower(raw.get("condition_id") or raw.get("conditionId") or raw.get("market"))
            asset = _lower(raw.get("asset_id") or raw.get("token_id") or raw.get("asset"))
            slug = slug_map.get((tx, condition, asset)) or slug_map.get((tx, condition, "")) or slug_map.get((tx, "", asset))
            if not slug or not BTC5M_RE.search(slug):
                continue
            matched = _timestamp_us(raw.get("matched_at") or raw.get("matchedAt") or raw.get("timestamp"))
            price = _finite(raw.get("price"))
            size = _finite(raw.get("size"))
            side = _lower(raw.get("side") or raw.get("trader_side"))
            if matched is None or not condition or not asset or price is None or size is None or size <= 0 or not 0 < price < 1:
                continue
            if side not in {"buy", "sell"}:
                continue
            output.append({
                "account_label": label,
                "owner_digest": owner_digest,
                "slug": slug,
                "market": condition,
                "asset_id": asset,
                "transaction_hash": tx,
                "matched_at_us": matched,
                "trade_price": price,
                "trade_size": size,
                "side": side,
                "fee_rate_bps": _finite(raw.get("fee_rate_bps")),
            })
    output.sort(key=lambda row: (row["matched_at_us"], row["account_label"], row["transaction_hash"]))
    if len(output) > MAX_TRADES:
        output = output[:MAX_TRADES]
    return output


def _query_hour(url: str, trades: list[dict[str, Any]], *, max_rows: int = MAX_ROWS_PER_HOUR) -> list[dict[str, Any]]:
    """Read only matching rows from a remote PMXT Parquet file in cloud."""
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised by workflow contract
        raise JoinError("duckdb_unavailable") from exc
    markets = sorted({row["market"] for row in trades})
    assets = sorted({row["asset_id"] for row in trades})
    start_us = min(row["matched_at_us"] for row in trades) - MAX_SNAPSHOT_AGE_US
    end_us = max(row["matched_at_us"] for row in trades) + 1_000_000
    market_marks = ",".join("?" for _ in markets)
    asset_marks = ",".join("?" for _ in assets)
    sql = f"""
        SELECT CAST(market AS VARCHAR) AS market,
               CAST(asset_id AS VARCHAR) AS asset_id,
               event_type,
               epoch_us(timestamp) AS event_us,
               epoch_us(timestamp_received) AS received_us,
               bids,
               asks
        FROM read_parquet(?, binary_as_string=true)
        WHERE event_type = 'book'
          AND CAST(market AS VARCHAR) IN ({market_marks})
          AND CAST(asset_id AS VARCHAR) IN ({asset_marks})
          AND timestamp_received >= to_timestamp(? / 1000000.0)
          AND timestamp_received <= to_timestamp(? / 1000000.0)
        ORDER BY received_us
        LIMIT {int(max_rows)}
    """
    con = duckdb.connect(database=":memory:")
    try:
        rows = con.execute(sql, [url, *markets, *assets, start_us, end_us]).fetchall()
        columns = [item[0] for item in con.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        con.close()


def align(h279: Mapping[str, Any], *, base_url: str = PMXT_V2_BASE) -> dict[str, Any]:
    trades = normalize_trades(h279)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(_iso_hour(trade["matched_at_us"]), []).append(trade)
    hours = sorted(grouped)
    source_failures: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    queried_urls: list[str] = []
    if len(hours) > MAX_HOURS:
        source_failures.append({"code": "hour_budget_exceeded", "hours": len(hours), "max_hours": MAX_HOURS})
        hours = hours[:MAX_HOURS]
    for hour in hours:
        url = f"{base_url.rstrip('/')}/polymarket_orderbook_{hour}.parquet"
        queried_urls.append(url)
        try:
            snapshots.extend(_query_hour(url, grouped[hour]))
        except Exception as exc:  # keep the failure sealed and continue other hours
            source_failures.append({"code": "source_query_failed", "hour": hour, "error_type": type(exc).__name__})
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        market = _lower(snapshot.get("market"))
        asset = _lower(snapshot.get("asset_id"))
        if market and asset:
            by_key.setdefault((market, asset), []).append(snapshot)
    for values in by_key.values():
        values.sort(key=lambda row: int(row.get("received_us") or 0))
    matches: list[dict[str, Any]] = []
    for trade in trades:
        candidates = by_key.get((trade["market"], trade["asset_id"]), [])
        prior = [row for row in candidates if _finite(row.get("received_us")) is not None and int(row["received_us"]) <= trade["matched_at_us"]]
        snapshot = prior[-1] if prior else None
        result = {
            "account_label": trade["account_label"],
            "owner_digest": trade["owner_digest"],
            "slug": trade["slug"],
            "market": trade["market"],
            "asset_id": trade["asset_id"],
            "transaction_hash": trade["transaction_hash"],
            "matched_at_us": trade["matched_at_us"],
            "trade_price": trade["trade_price"],
            "trade_size": trade["trade_size"],
            "side": trade["side"],
            "fee_rate_bps": trade["fee_rate_bps"],
            "book_snapshot_found": snapshot is not None,
            "paper_fill_admitted": False,
            "evidence_boundary": "owner_trade_to_public_book_alignment_only",
        }
        if snapshot is None:
            result["missing_reason"] = "no_prior_book_snapshot"
            matches.append(result)
            continue
        received_us = int(snapshot["received_us"])
        event_us = int(snapshot["event_us"]) if snapshot.get("event_us") is not None else None
        age_us = trade["matched_at_us"] - received_us
        bids, asks = _levels(snapshot.get("bids")), _levels(snapshot.get("asks"))
        levels = asks if trade["side"] == "buy" else bids
        ladder = _ladder_cover(levels, asks=trade["side"] == "buy", trade_price=trade["trade_price"], trade_size=trade["trade_size"])
        result.update({
            "snapshot_event_us": event_us,
            "snapshot_received_us": received_us,
            "snapshot_age_ms": age_us / 1000.0,
            "event_to_receive_ms": ((received_us - event_us) / 1000.0) if event_us is not None else None,
            "snapshot_age_within_bound": 0 <= age_us <= MAX_SNAPSHOT_AGE_US,
            "event_lag_within_bound": event_us is None or abs(trade["matched_at_us"] - event_us) <= MAX_EVENT_LAG_US,
            **ladder,
            "book_levels_bid": len(bids),
            "book_levels_ask": len(asks),
        })
        # This is deliberately stricter than a price-touch check, but remains
        # a paper diagnostic and is never relabeled as an authenticated fill.
        result["paper_fill_admitted"] = bool(
            result["snapshot_age_within_bound"]
            and result["event_lag_within_bound"]
            and result["displayed_size_covers_trade"]
            and result["trade_price_touches_displayed_ladder"]
        )
        matches.append(result)
    matched = [row for row in matches if row["book_snapshot_found"]]
    admitted = [row for row in matches if row.get("paper_fill_admitted")]
    return {
        "format": FORMAT,
        "protocol": {
            "read_only": True,
            "no_order_submission": True,
            "no_account_mutation": True,
            "paper_fill_admission_is_not_authenticated_fill": True,
            "snapshot_age_bound_seconds": MAX_SNAPSHOT_AGE_US / 1_000_000,
            "max_event_lag_seconds": MAX_EVENT_LAG_US / 1_000_000,
        },
        "source": {
            "provider": "PMXT v2 public hourly Parquet",
            "base_url": base_url,
            "queried_hours": hours,
            "queried_url_sha256": _digest(queried_urls),
            "query_count": len(queried_urls),
        },
        "counts": {
            "normalized_btc5m_owner_trades": len(trades),
            "book_rows_returned": len(snapshots),
            "book_snapshot_matched": len(matched),
            "paper_fill_admitted": len(admitted),
            "source_failures": len(source_failures),
        },
        "matches": matches,
        "source_failures": source_failures,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "strategy_evaluated": False,
        "model_refit": False,
        "new_data_collected": True,
        "same_ledger_compounding_rows": 0,
        "promotion_eligible": False,
        "evidence_boundary": "public_book_alignment_is_not_execution_or_strategy_edge",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--h279", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default=PMXT_V2_BASE)
    args = parser.parse_args()
    h279 = json.loads(args.h279.read_text(encoding="utf-8"))
    result = align(h279, base_url=args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": FORMAT, "trades": result["counts"]["normalized_btc5m_owner_trades"], "matched": result["counts"]["book_snapshot_matched"], "live_orders_enabled": False}, sort_keys=True))


if __name__ == "__main__":
    main()
