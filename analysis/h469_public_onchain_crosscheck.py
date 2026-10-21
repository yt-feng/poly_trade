"""H469: public on-chain cross-check for the sealed H468 paper chain.

This uses the same immutable PMData archive and recomputes the frozen H468
queue-proxy chain.  It then asks a narrower causal question: does each proxy
fill have public on-chain fill activity for the same market/outcome, within a
pre-registered two-second timestamp and one-tick price window, with at least
the proxy quantity?  A match is public execution support only; it is never an
owner-scoped fill or cash receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import h468_pmdata_python_event_replay as h468

PUBLIC_TIME_WINDOW_SECONDS = 2.0
PRICE_TOLERANCE = 0.0015


def _public_match_from_rows(proxy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0.0
    count = 0
    for row in rows:
        if str(row.get("market_slug")) != str(proxy["market_slug"]):
            continue
        if str(row.get("outcome", "")).strip().lower() != str(proxy["outcome"]).lower():
            continue
        if abs(float(row["timestamp_seconds"]) - float(proxy["fill_ts"])) > PUBLIC_TIME_WINDOW_SECONDS:
            continue
        if abs(float(row["price"]) - float(proxy["exec_price"])) > PRICE_TOLERANCE:
            continue
        count += 1
        total += float(row.get("token_amount", 0.0) or 0.0)
    quantity = float(proxy["exec_qty"])
    return {"matching_rows": count, "matching_token_amount": total, "quantity_covered": total + 1e-12 >= quantity, "public_match": count > 0 and total + 1e-12 >= quantity}


def crosscheck(l2_path: Path, trades_path: Path, markets_path: Path, fills_path: Path | None, output: Path) -> dict[str, Any]:
    import polars as pl

    replay_dir = output / "h468-replay"
    h468_report = h468.replay(l2_path, trades_path, markets_path, fills_path, replay_dir)
    chain = h468_report["paper"]["chain"]
    if fills_path is None or not fills_path.exists():
        raise ValueError("onchain fills archive is required for H469")
    fills = pl.read_parquet(fills_path).with_columns(
        pl.col("timestamp").dt.epoch("us").cast(pl.Float64).truediv(1_000_000).alias("timestamp_seconds"),
        pl.col("outcome").cast(pl.Utf8).str.to_lowercase().alias("_outcome"),
    )
    matches: list[dict[str, Any]] = []
    for proxy in chain:
        candidates = fills.filter(
            (pl.col("market_slug").cast(pl.Utf8) == str(proxy["market_slug"]))
            & (pl.col("_outcome") == str(proxy["outcome"]).lower())
            & ((pl.col("timestamp_seconds") - float(proxy["fill_ts"])).abs() <= PUBLIC_TIME_WINDOW_SECONDS)
            & ((pl.col("price") - float(proxy["exec_price"])).abs() <= PRICE_TOLERANCE)
        )
        amount = float(candidates.get_column("token_amount").sum() or 0.0)
        count = int(candidates.height)
        matches.append({"sequence": proxy["sequence"], "market_slug": proxy["market_slug"], "outcome": proxy["outcome"], "matching_rows": count, "matching_token_amount": amount, "quantity_covered": amount + 1e-12 >= float(proxy["exec_qty"]), "public_match": count > 0 and amount + 1e-12 >= float(proxy["exec_qty"])})
    report = {
        "format": "old-pmdata-h469-public-onchain-crosscheck-v1",
        "hypothesis_id": "H469_PMDATA_PUBLIC_ONCHAIN_SUPPORT_FOR_H468_CHAIN",
        "source": {"dataset_revision": h468.DATASET_REVISION, "dataset_source_repo_commit": h468.DATASET_SOURCE_REPO_COMMIT, "fixed_archive": True, "onchain_fill_rows": int(fills.height)},
        "protocol": {"public_time_window_seconds": PUBLIC_TIME_WINDOW_SECONDS, "price_tolerance": PRICE_TOLERANCE, "required_proxy_fills": h468.REQUIRED_FILLS, "same_ledger_inherited": True, "paper_only": True, "owner_scoped_receipts_required": True},
        "h468_paper": {"fills": h468_report["paper"]["fills"], "first_ten_passed": h468_report["paper"]["first_ten_passed"], "ending_cash_usd": h468_report["paper"]["ending_cash_usd"]},
        "public_crosscheck": {"proxy_fills": len(chain), "matched_proxy_fills": sum(1 for x in matches if x["public_match"]), "all_proxy_fills_matched": len(matches) == h468.REQUIRED_FILLS and all(x["public_match"] for x in matches), "matches": matches},
        "new_market_data_collected": False, "model_refit": False, "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "evidence_boundary": "public on-chain fill support only; no owner, account order, settlement receipt, independent cash release or balance checkpoint",
    }
    output.mkdir(parents=True, exist_ok=True)
    path = output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report["report_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--l2", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--fills", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(crosscheck(args.l2, args.trades, args.markets, args.fills, args.output), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
