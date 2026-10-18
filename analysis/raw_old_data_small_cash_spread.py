"""Replay a predeclared causal spread cap directly from the clean old quotes.

The current-main walk-forward uses a $100 immediate-cash simulator.  This
diagnostic rebuilds the minute-one prefix from the raw clean tables, requires
the target to be visible at the decision timestamp, and replays the same
signal under the frozen $10/$5/0.07/0.5%/600-second cash contract.  It uses
the observed minute-one bid size with the existing 25% fill-ratio lower bound;
it never submits orders and never counts simulated fills as canaries.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

import all_monthly_clob_systematic_research_v2 as base

FILL_RATIO = 0.25
SETTLEMENT_SECONDS = 300.0
BLOCK_SIZES = (16, 16, 16, 16, 15)


def _first_non_null(s: pd.Series) -> float:
    x = s.dropna()
    return float(x.iloc[0]) if not x.empty else math.nan


def _last_non_null(s: pd.Series) -> float:
    x = s.dropna()
    return float(x.iloc[-1]) if not x.empty else math.nan


def build_snapshots(source_root: Path) -> tuple[pd.DataFrame, list[str], pd.DataFrame]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"].eq(maximum), "run_name"].astype(str).tolist()
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", dropna=False, sort=False):
        g = group.sort_values("ts_utc").reset_index(drop=True)
        first_ts = g["ts_utc"].min()
        prefix = g[g["ts_utc"].le(first_ts + pd.Timedelta(minutes=1))]
        if prefix.empty:
            continue
        snap = prefix.iloc[-1]
        target = _first_non_null(prefix["target_price"])
        final = _last_non_null(g["final_price"])
        if not math.isfinite(target) or not math.isfinite(final):
            continue
        buy = pd.to_numeric(snap.get("buy_down_cents"), errors="coerce")
        size = pd.to_numeric(snap.get("buy_down_size"), errors="coerce")
        spread = pd.to_numeric(snap.get("spread_down_cents"), errors="coerce")
        btc = pd.to_numeric(snap.get("final_price"), errors="coerce")
        if any(pd.isna(x) for x in (buy, size, spread, btc)):
            continue
        rows.append(
            {
                "market_id": str(market_id),
                "run_name": str(snap["run_name"]),
                "slug": str(snap["slug"]),
                "first_quote_ts": first_ts,
                "target_visible_at_m1": target,
                "final_price": final,
                "outcome_up": float(final > target),
                "btc_move_1m": float(btc - target),
                "buy_down_price_m1": float(buy) / 100.0,
                "buy_down_size_m1": float(size),
                "spread_down_1m": float(spread) / 100.0,
            }
        )
    frame = pd.DataFrame(rows).sort_values("first_quote_ts").reset_index(drop=True)
    return frame, runs, coverage


def fee(shares: float, price: float) -> float:
    return round(float(shares) * 0.07 * (float(price) ** 1.0) * (1.0 - float(price)), 5)


def replay(frame: pd.DataFrame, label: str, initial_cash: float, min_cost: float,
           slippage: float, cash_release_delay: float) -> tuple[dict[str, object], pd.DataFrame]:
    d = frame.sort_values("first_quote_ts").reset_index(drop=True)
    cash = float(initial_cash)
    positions: deque[dict[str, float]] = deque()
    trades: list[dict[str, object]] = []
    blocked_cash = 0
    blocked_capacity = 0
    peak_cash = cash
    max_drawdown = 0.0
    for _, row in d.iterrows():
        now = pd.Timestamp(row["first_quote_ts"]).timestamp()
        while positions and positions[0]["release"] <= now + 1e-9:
            cash += positions.popleft()["payout"]
        price = float(row["buy_down_price_m1"])
        fill_price = price + float(slippage)
        capacity = float(row["buy_down_size_m1"]) * FILL_RATIO * fill_price
        target_cost = cash * 0.04
        required = max(float(min_cost), 5.0 * fill_price)
        if cash + 1e-12 < required:
            blocked_cash += 1
            continue
        if capacity + 1e-12 < required:
            blocked_capacity += 1
            continue
        cost = min(max(target_cost, required), cash, capacity)
        if not math.isfinite(cost) or cost < required or fill_price <= 0 or fill_price >= 0.98:
            blocked_capacity += 1
            continue
        shares = cost / fill_price
        entry_fee = fee(shares, fill_price)
        reserved = cost + entry_fee
        if reserved > cash + 1e-12:
            blocked_cash += 1
            continue
        payout = shares * (1.0 - float(row["outcome_up"]))
        pnl = payout - reserved
        cash -= reserved
        positions.append({
            "release": now + SETTLEMENT_SECONDS + float(cash_release_delay),
            "payout": payout,
        })
        peak_cash = max(peak_cash, cash)
        max_drawdown = max(max_drawdown, (peak_cash - cash) / peak_cash if peak_cash else 0.0)
        trades.append({
            "label": label,
            "run_name": row["run_name"],
            "market_id": row["market_id"],
            "first_quote_ts": row["first_quote_ts"],
            "entry_price": price,
            "fill_price": fill_price,
            "quote_size": float(row["buy_down_size_m1"]),
            "capacity": capacity,
            "cost": cost,
            "fee": entry_fee,
            "outcome_up": float(row["outcome_up"]),
            "pnl": pnl,
            "cash_after_entry": cash,
        })
    while positions:
        cash += positions.popleft()["payout"]
    return {
        "label": label,
        "trades": len(trades),
        "blocked_cash": blocked_cash,
        "blocked_capacity": blocked_capacity,
        "ending_cash": cash,
        "net_pnl": cash - initial_cash,
        "max_drawdown": max_drawdown,
    }, pd.DataFrame(trades)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--initial-cash", type=float, default=10.0)
    ap.add_argument("--min-cost", type=float, default=5.0)
    ap.add_argument("--slippage", type=float, default=0.005)
    ap.add_argument("--cash-release-delay", type=float, default=600.0)
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    frame, runs, coverage = build_snapshots(Path(args.source_root))
    base_signal = frame["btc_move_1m"].le(-20.0) & frame["spread_down_1m"].le(0.06)
    tight_signal = base_signal & frame["spread_down_1m"].le(0.04)
    candidates = {
        "base_drop_spread06": frame[base_signal].copy(),
        "tight_drop_spread04": frame[tight_signal].copy(),
    }
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    summary: list[dict[str, object]] = []
    blocks: list[dict[str, object]] = []
    selection: list[dict[str, object]] = []
    for label, candidate in candidates.items():
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            metrics, trades = replay(candidate[candidate["run_name"].isin(names)], label, args.initial_cash,
                                     args.min_cost, args.slippage, args.cash_release_delay)
            metrics.update({"split": split, "run_count": len(names)})
            summary.append(metrics)
            if split == "selection20":
                selection.append(metrics)
            if not trades.empty:
                trades.to_csv(out / f"{label}_{split}_trades.csv", index=False)
        cursor = 0
        for block_index, size in enumerate(BLOCK_SIZES, 1):
            names = test_runs[cursor:cursor + size]
            cursor += size
            metrics, _ = replay(candidate[candidate["run_name"].isin(names)], label, args.initial_cash,
                                args.min_cost, args.slippage, args.cash_release_delay)
            blocks.append({"label": label, "block": block_index, "run_count": size,
                           "first_run": names[0], "last_run": names[-1], **metrics})
    result = {
        "format": "old-data-raw-causal-small-cash-spread-v1",
        "source_root": str(args.source_root),
        "complete_runs": len(runs),
        "usable_m1_markets": int(len(frame)),
        "target_visible_at_decision_required": True,
        "candidates": {
            "base_drop_spread06": "btc_move_1m <= -20 and spread_down_1m <= 0.06",
            "tight_drop_spread04": "base plus spread_down_1m <= 0.04",
        },
        "protocol": {
            "initial_cash": args.initial_cash,
            "min_cost": args.min_cost,
            "fee_rate": 0.07,
            "fee_exponent": 1.0,
            "slippage": args.slippage,
            "settlement_seconds": SETTLEMENT_SECONDS,
            "cash_release_delay_seconds": args.cash_release_delay,
            "fill_ratio": FILL_RATIO,
            "fraction": 0.04,
            "capacity_basis": "observed minute-one buy_down_size times 25% fill ratio",
        },
        "splits": {"selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs), "test_runs": len(test_runs)},
        "promotion_rule": "all five independent blocks must end at >= $5 with nonnegative PnL; this remains paper-only",
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "summary": summary,
        "blocks": blocks,
    }
    pd.DataFrame(summary).to_csv(out / "summary.csv", index=False)
    pd.DataFrame(selection).to_csv(out / "selection.csv", index=False)
    pd.DataFrame(blocks).to_csv(out / "blocks.csv", index=False)
    out.joinpath("meta.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
