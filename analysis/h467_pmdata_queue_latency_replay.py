"""H467: one fixed PMData queue/latency replay on a single evolving $10 ledger.

This is an execution diagnostic, not a model search.  The entry rule is frozen
to the public pm-hftbacktest endgame trigger: submit one five-share limit order
when either binary outcome's mid reaches 0.84.  The order is exposed to the
repository-pinned queue and latency implementation, then the external ledger
applies the documented binary fee, settlement and cash-release clock.

The PMData archive is immutable historical source material.  Its public fills
are never treated as this account's fills, and the report cannot produce a
Canary row without owner-scoped receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

DATASET_REVISION = "acb8cecf4234677823ddd8ee1275de1db48aa995"
DATASET_SOURCE_REPO_COMMIT = "b7f29614a695740c2102760a4dacfb85b8d6e623"
PM_HFTBACKTEST_COMMIT = "7f08791d2efe830e10ec80a8dc91956e0b0f65fe"
INITIAL_CASH = 10.0
MINIMUM_SHARES = 5.0
TRIGGER = 0.84
TAKER_FEE_RATE = 0.07
ENTRY_LATENCY_NS = 20_000_000
RESPONSE_LATENCY_NS = 20_000_000
CASH_RELEASE_DELAY_SECONDS = 600.0
REQUIRED_FILLS = 10
STEP_NS = 100_000_000


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _epoch_seconds(value: Any) -> float:
    ts = value
    if hasattr(ts, "timestamp"):
        return float(ts.timestamp())
    return float(ts)


def _winning_outcome(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"yes", "up", "true", "1"}:
        return "yes"
    if text in {"no", "down", "false", "0"}:
        return "no"
    return None


def _invert_winning_outcome(value: Any) -> Any:
    """Map the source market outcome onto the complementary token book."""
    outcome = _winning_outcome(value)
    if outcome == "yes":
        return "no"
    if outcome == "no":
        return "yes"
    return value


def _invert_l2_book(l2: Any) -> Any:
    """Build the complementary binary-token book from PMData's market book.

    PMData stores one market-level book (there is no ``outcome`` column in
    the L2 archive).  The no-token book is the price complement of the yes
    book: bids become complemented asks and asks become complemented bids.
    ``polymarket_to_hbt`` can then run the two tokens as independent assets.
    """
    import polars as pl

    return l2.with_columns(
        pl.col("ask_prices").list.eval(1.0 - pl.element()).alias("_no_bid_prices"),
        pl.col("bid_prices").list.eval(1.0 - pl.element()).alias("_no_ask_prices"),
        pl.col("ask_sizes").alias("_no_bid_sizes"),
        pl.col("bid_sizes").alias("_no_ask_sizes"),
        (1.0 - pl.col("best_ask")).alias("_no_best_bid"),
        (1.0 - pl.col("best_bid")).alias("_no_best_ask"),
        pl.when(pl.col("pc_side") == "BUY")
        .then(pl.lit("SELL"))
        .when(pl.col("pc_side") == "SELL")
        .then(pl.lit("BUY"))
        .otherwise(pl.col("pc_side"))
        .alias("_no_pc_side"),
        pl.when(pl.col("winning_outcome").cast(pl.Utf8).str.to_lowercase() == "yes")
        .then(pl.lit("no"))
        .when(pl.col("winning_outcome").cast(pl.Utf8).str.to_lowercase() == "no")
        .then(pl.lit("yes"))
        .otherwise(pl.col("winning_outcome"))
        .alias("_no_winning_outcome"),
        (1.0 - pl.col("pc_price")).alias("_no_pc_price"),
    ).with_columns(
        pl.col("_no_bid_prices").alias("bid_prices"),
        pl.col("_no_ask_prices").alias("ask_prices"),
        pl.col("_no_bid_sizes").alias("bid_sizes"),
        pl.col("_no_ask_sizes").alias("ask_sizes"),
        pl.col("_no_best_bid").alias("best_bid"),
        pl.col("_no_best_ask").alias("best_ask"),
        pl.col("_no_pc_side").alias("pc_side"),
        pl.col("_no_winning_outcome").alias("winning_outcome"),
        pl.col("_no_pc_price").alias("pc_price"),
    ).drop(
        "_no_bid_prices", "_no_ask_prices", "_no_bid_sizes", "_no_ask_sizes",
        "_no_best_bid", "_no_best_ask", "_no_pc_side", "_no_winning_outcome",
        "_no_pc_price",
    )


def _run_market(l2: pl.DataFrame, trades: pl.DataFrame, trigger: float) -> dict[str, Any]:
    """Run the frozen two-token trigger through pm-hftbacktest."""
    # The import is intentionally lazy so contract and ledger tests do not
    # require a Rust wheel on the developer machine.
    import numpy as np
    import polars as pl
    from numba import njit
    from hftbacktest import (
        BacktestAssetPoly,
        GTC,
        LIMIT,
        ROIVectorMarketDepthBacktest,
        polymarket_to_hbt,
    )

    @njit
    def strategy(hbt, trigger_price):
        submitted = False
        for _ in range(200_000):
            if hbt.elapse(STEP_NS) != 0:
                break
            if submitted:
                continue
            for asset_no in range(2):
                depth = hbt.depth(asset_no)
                bid = depth.best_bid
                ask = depth.best_ask
                if bid <= 0.0 or ask <= 0.0 or ask >= 1.0:
                    continue
                mid = (bid + ask) / 2.0
                if mid >= trigger_price:
                    price = round(trigger_price / depth.tick_size) * depth.tick_size
                    price = min(max(price, depth.tick_size), 1.0 - depth.tick_size)
                    hbt.submit_buy_order(
                        asset_no, np.uint64(asset_no + 1), price,
                        np.float64(MINIMUM_SHARES), GTC, LIMIT, False,
                    )
                    submitted = True
                    break

    assets = []
    token_books = {"yes": l2, "no": _invert_l2_book(l2)}
    for outcome in ("yes", "no"):
        part = token_books[outcome]
        if part.is_empty():
            return {"status": "invalid_market", "reason": f"missing_{outcome}_book"}
        if trades.is_empty() or "outcome" not in trades.columns:
            trade_part = pl.DataFrame()
        else:
            trade_part = trades.filter(pl.col("outcome").cast(pl.Utf8).str.to_lowercase() == outcome)
        data = polymarket_to_hbt(part, trade_df=trade_part if not trade_part.is_empty() else None)
        if len(data) == 0:
            return {"status": "invalid_market", "reason": f"empty_{outcome}_events"}
        assets.append(
            BacktestAssetPoly()
            .data(data)
            .constant_order_latency(ENTRY_LATENCY_NS, RESPONSE_LATENCY_NS)
            .binary_fee_model(0.0, TAKER_FEE_RATE)
        )
    hbt = ROIVectorMarketDepthBacktest(assets)
    try:
        strategy(hbt, float(trigger))

        filled: list[dict[str, Any]] = []
        for asset_no in range(2):
            state = hbt.state_values(asset_no)
            if int(state.num_trades) <= 0 or float(state.position) <= 0.0:
                continue
            # Read aggregate state instead of walking the Rust-backed order
            # dictionary from Python.  The latter is a Numba jitclass API
            # intended for an ``@njit`` strategy and can invalidate native
            # pointers when iterated from the Python boundary.
            volume = float(state.trading_volume)
            value = float(state.trading_value)
            if volume <= 0.0 or value <= 0.0:
                continue
            exec_price = value / volume
            if not math.isfinite(exec_price) or exec_price <= 0.0:
                continue
            filled.append({
                "asset_no": asset_no,
                "outcome": ("yes", "no")[asset_no],
                "exec_qty": volume,
                "exec_price": exec_price,
                "order_status": "aggregate_state_filled",
                "cost": value,
                "fee": float(state.fee),
                "num_trades": int(state.num_trades),
            })
            break
        if not filled:
            return {"status": "no_fill", "submitted": True}
        return {"status": "filled", "fill": filled[0]}
    finally:
        # The Rust-backed object owns native allocations.  Explicit close is
        # required when a market is replayed in a loop; relying on Python's
        # destructor caused the prior Action run to segfault at market exit.
        hbt.close()


def replay(l2_path: Path, trades_path: Path, markets_path: Path, fills_path: Path | None, output: Path, trigger: float = TRIGGER) -> dict[str, Any]:
    import polars as pl
    markets = pl.read_parquet(markets_path).sort("start_time")
    l2 = pl.read_parquet(l2_path)
    trades = pl.read_parquet(trades_path)
    onchain_rows = None
    if fills_path is not None and fills_path.exists():
        onchain_rows = int(pl.read_parquet(fills_path).height)
    market_names = set(markets.get_column("market_slug").cast(pl.Utf8).to_list())
    l2 = l2.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(market_names)))
    trades = trades.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(market_names)))
    archive_markets = set(l2.get_column("market_slug").cast(pl.Utf8).unique().to_list())
    markets = markets.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(archive_markets)))
    def _partitioned(frame: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in frame.partition_by("market_slug", as_dict=True).items():
            normalized = key[0] if isinstance(key, tuple) else key
            out[str(normalized)] = value
        return out

    by_market_l2 = _partitioned(l2)
    by_market_trades = _partitioned(trades)

    cash = INITIAL_CASH
    available_at = -float("inf")
    chain: list[dict[str, Any]] = []
    skipped_before_release = 0
    no_fill = 0
    insufficient_cash = 0
    for row in markets.iter_rows(named=True):
        slug = str(row["market_slug"])
        start_ts = _epoch_seconds(row["start_time"])
        end_ts = _epoch_seconds(row["end_time"])
        if start_ts < available_at:
            skipped_before_release += 1
            continue
        result = _run_market(by_market_l2[slug], by_market_trades.get(slug, pl.DataFrame()), trigger)
        if result.get("status") == "no_fill":
            no_fill += 1
            continue
        if result.get("status") != "filled":
            continue
        fill = result["fill"]
        total = float(fill["cost"]) + float(fill["fee"])
        if total > cash + 1e-12:
            insufficient_cash += 1
            continue
        before = cash
        cash -= total
        winner = _winning_outcome(row.get("winning_outcome"))
        payout = float(fill["exec_qty"]) if winner == fill["outcome"] else 0.0
        cash_after_settlement = cash + payout
        release_ts = end_ts + CASH_RELEASE_DELAY_SECONDS
        cash = cash_after_settlement
        available_at = release_ts
        chain.append({
            "sequence": len(chain) + 1,
            "market_slug": slug,
            "entry_ts": start_ts,
            "release_ts": release_ts,
            "outcome": fill["outcome"],
            "winner": bool(payout > 0),
            "exec_qty": float(fill["exec_qty"]),
            "exec_price": float(fill["exec_price"]),
            "cost": float(fill["cost"]),
            "fee": float(fill["fee"]),
            "cash_before": before,
            "cash_after": cash,
        })
        if len(chain) >= REQUIRED_FILLS:
            break

    first_ten = chain[:REQUIRED_FILLS]
    report = {
        "format": "old-pmdata-h467-queue-latency-replay-v1",
        "hypothesis_id": "H467_PMDATA_FIXED_ENDGAME_QUEUE_LATENCY",
        "dataset_revision": DATASET_REVISION,
        "dataset_source_repo_commit": DATASET_SOURCE_REPO_COMMIT,
        "pm_hftbacktest_commit": PM_HFTBACKTEST_COMMIT,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_shares": MINIMUM_SHARES,
            "trigger_mid": trigger,
            "entry_latency_ns": ENTRY_LATENCY_NS,
            "response_latency_ns": RESPONSE_LATENCY_NS,
            "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "required_consecutive_fills": REQUIRED_FILLS,
            "same_ledger": True,
            "no_reset": True,
            "paper_only": True,
        },
        "source": {
            "l2_rows": int(l2.height),
            "trade_rows": int(trades.height),
            "onchain_fill_rows": onchain_rows,
            "markets": int(markets.height),
            "fixed_archive": True,
        },
        "paper": {
            "fills": len(chain),
            "first_ten_fills": len(first_ten),
            "first_ten_passed": len(first_ten) == REQUIRED_FILLS,
            "ending_cash_usd": cash,
            "first_ten_ending_cash_usd": first_ten[-1]["cash_after"] if len(first_ten) == REQUIRED_FILLS else cash,
            "skipped_before_release": skipped_before_release,
            "no_fill_markets": no_fill,
            "insufficient_cash_fills": insufficient_cash,
            "chain": first_ten,
        },
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "fixed public PMData paper replay; no owner-scoped order, settlement, cash-release or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--l2", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--fills", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trigger", type=float, default=TRIGGER)
    args = parser.parse_args()
    print(json.dumps(replay(args.l2, args.trades, args.markets, args.fills, args.output, args.trigger), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
