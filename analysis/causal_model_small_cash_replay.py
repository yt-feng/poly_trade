"""Expanding-window causal probability model under the frozen $10 contract.

This is an old-clean-dataset paper diagnostic.  Each market's minute-one
features require the target price to be visible by the decision timestamp.
The logistic model is fit only on earlier complete runs before predicting the
current run.  A small predeclared threshold/price/spread grid is selected on
the first 20 test runs and then evaluated on the 59-run holdout and five
independent blocks.  No order is submitted and no simulated fill is a canary.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import all_monthly_clob_systematic_research_v2 as base

FILL_RATIO = 0.25
SETTLEMENT_PLUS_RELEASE = 900.0
BLOCK_SIZES = (16, 16, 16, 16, 15)
GRID_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
GRID_PRICE_CAPS = (0.50, 0.55, 0.60, 0.65, 0.70)
GRID_SPREAD_CAPS = (0.04, 0.06)


def _first_non_null(series: pd.Series) -> float:
    values = series.dropna()
    return float(values.iloc[0]) if not values.empty else math.nan


def _last_non_null(series: pd.Series) -> float:
    values = series.dropna()
    return float(values.iloc[-1]) if not values.empty else math.nan


def build_m1(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"].eq(maximum), "run_name"].astype(str).tolist()
    quotes = base.prepare_quotes(raw[raw["run_name"].astype(str).isin(runs)].copy())
    columns = [
        "buy_up_cents", "buy_down_cents", "sell_up_cents", "sell_down_cents",
        "buy_up_size", "buy_down_size", "sell_up_size", "sell_down_size",
        "mid_up_cents", "mid_down_cents", "spread_up_cents", "spread_down_cents",
        "bid_depth_up_5", "ask_depth_up_5", "bid_depth_down_5", "ask_depth_down_5",
        "trade_count_1s", "trade_volume_1s",
    ]
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", sort=False):
        group = group.sort_values("ts_utc")
        first_ts = group["ts_utc"].min()
        prefix = group[group["ts_utc"].le(first_ts + pd.Timedelta(minutes=1))]
        if prefix.empty:
            continue
        snapshot = prefix.iloc[-1]
        target = _first_non_null(prefix["target_price"])
        final = _last_non_null(group["final_price"])
        if not math.isfinite(target) or not math.isfinite(final) or pd.isna(snapshot.get("final_price")):
            continue
        row: dict[str, object] = {
            "market_id": str(market_id),
            "run_name": str(snapshot["run_name"]),
            "first_quote_ts": first_ts,
            "outcome_up": float(final > target),
            "btc_move_1m": float(snapshot["final_price"] - target),
        }
        for column in columns:
            value = pd.to_numeric(snapshot.get(column), errors="coerce")
            row[column] = float(value) if pd.notna(value) else math.nan
        rows.append(row)
    if not rows:
        raise RuntimeError("NO_CAUSAL_M1_MARKETS")
    return pd.DataFrame(rows).sort_values("first_quote_ts").reset_index(drop=True), runs


def add_features(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    frame = frame.copy()
    timestamp = pd.to_datetime(frame["first_quote_ts"], utc=True)
    hour = timestamp.dt.hour + timestamp.dt.minute / 60.0
    frame["hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
    frame["hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
    frame["dow_sin"] = np.sin(2.0 * np.pi * timestamp.dt.dayofweek / 7.0)
    frame["dow_cos"] = np.cos(2.0 * np.pi * timestamp.dt.dayofweek / 7.0)
    frame["up_mid"] = frame["mid_up_cents"] / 100.0
    frame["down_mid"] = frame["mid_down_cents"] / 100.0
    frame["overround"] = (frame["mid_up_cents"] + frame["mid_down_cents"] - 100.0) / 100.0
    frame["price_gap"] = frame["buy_up_cents"] / 100.0 + frame["buy_down_cents"] / 100.0 - 1.0
    frame["size_imbalance"] = (frame["buy_up_size"] - frame["buy_down_size"]) / (
        frame["buy_up_size"] + frame["buy_down_size"]
    ).replace(0, np.nan)
    frame["depth_imbalance"] = (frame["bid_depth_up_5"] - frame["bid_depth_down_5"]) / (
        frame["bid_depth_up_5"] + frame["bid_depth_down_5"]
    ).replace(0, np.nan)
    numeric = [
        "btc_move_1m", "buy_up_cents", "buy_down_cents", "sell_up_cents", "sell_down_cents",
        "buy_up_size", "buy_down_size", "sell_up_size", "sell_down_size", "mid_up_cents",
        "mid_down_cents", "spread_up_cents", "spread_down_cents", "bid_depth_up_5",
        "ask_depth_up_5", "bid_depth_down_5", "ask_depth_down_5", "trade_count_1s",
        "trade_volume_1s", "hour_sin", "hour_cos", "dow_sin", "dow_cos", "up_mid",
        "down_mid", "overround", "price_gap", "size_imbalance", "depth_imbalance",
    ]
    features = [column for column in numeric if frame[column].notna().any()]
    return frame, features


def expanding_probabilities(frame: pd.DataFrame, runs: list[str], features: list[str]) -> pd.Series:
    ordered = frame.sort_values("first_quote_ts").reset_index(drop=True)
    probabilities = np.full(len(ordered), np.nan)
    for index, run_name in enumerate(runs[4:], start=4):
        train = ordered[ordered["run_name"].isin(runs[:index])]
        current = ordered[ordered["run_name"].eq(run_name)]
        train = train[train["outcome_up"].notna()]
        if len(train) < 100 or train["outcome_up"].nunique() < 2 or current.empty:
            continue
        model = Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("logistic", LogisticRegression(C=0.5, max_iter=600)),
        ])
        model.fit(train[features], train["outcome_up"].astype(int))
        probabilities[current.index] = model.predict_proba(current[features])[:, 1]
    return pd.Series(probabilities, index=ordered.index)


def trade_fee(shares: float, price: float) -> float:
    return round(float(shares) * 0.07 * float(price) * (1.0 - float(price)), 5)


def replay(events: pd.DataFrame, run_names: list[str], slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    ordered = events[events["run_name"].isin(run_names)].sort_values("first_quote_ts")
    cash = 10.0
    positions: deque[tuple[float, float]] = deque()
    trades: list[dict[str, object]] = []
    blocked = 0
    peak = cash
    max_drawdown = 0.0
    for _, row in ordered.iterrows():
        now = pd.Timestamp(row["first_quote_ts"]).timestamp() + 60.0
        while positions and positions[0][0] <= now + 1e-9:
            cash += positions.popleft()[1]
        price = float(row["price"])
        fill_price = price + float(slippage)
        capacity = float(row["size"]) * FILL_RATIO * fill_price
        required = max(5.0, 5.0 * fill_price)
        if cash + 1e-12 < required or capacity + 1e-12 < required or fill_price >= 0.98:
            blocked += 1
            continue
        cost = min(max(cash * 0.04, required), cash, capacity)
        shares = cost / fill_price
        entry_fee = trade_fee(shares, fill_price)
        reserved = cost + entry_fee
        if reserved > cash + 1e-12:
            blocked += 1
            continue
        outcome = float(row["outcome_up"])
        payout = shares * (outcome if row["side"] == "up" else 1.0 - outcome)
        cash -= reserved
        positions.append((now + SETTLEMENT_PLUS_RELEASE, payout))
        trades.append({
            "run_name": row["run_name"], "market_id": row["market_id"], "side": row["side"],
            "first_quote_ts": row["first_quote_ts"], "price": price, "fill_price": fill_price,
            "size": float(row["size"]), "payout": payout, "fee": entry_fee,
            "outcome_up": outcome, "pnl": payout - reserved, "cash_after_entry": cash,
        })
        peak = max(peak, cash)
        max_drawdown = max(max_drawdown, (peak - cash) / peak if peak else 0.0)
    while positions:
        cash += positions.popleft()[1]
    metrics = {
        "trades": len(trades), "ending_cash": cash, "net_pnl": cash - 10.0,
        "blocked": blocked, "max_drawdown": max_drawdown,
    }
    return metrics, pd.DataFrame(trades)


def make_events(frame: pd.DataFrame, threshold: float, price_cap: float, spread_cap: float) -> pd.DataFrame:
    up = frame["p_up"].ge(threshold) & frame["buy_up_cents"].div(100.0).le(price_cap) & frame["spread_up_cents"].div(100.0).le(spread_cap)
    down = frame["p_down"].ge(threshold) & frame["buy_down_cents"].div(100.0).le(price_cap) & frame["spread_down_cents"].div(100.0).le(spread_cap)
    up_frame = frame.loc[up, ["market_id", "run_name", "first_quote_ts", "outcome_up", "buy_up_cents", "buy_up_size"]].copy()
    up_frame["side"] = "up"
    up_frame["price"] = up_frame["buy_up_cents"] / 100.0
    up_frame["size"] = up_frame["buy_up_size"]
    down_frame = frame.loc[down, ["market_id", "run_name", "first_quote_ts", "outcome_up", "buy_down_cents", "buy_down_size"]].copy()
    down_frame["side"] = "down"
    down_frame["price"] = down_frame["buy_down_cents"] / 100.0
    down_frame["size"] = down_frame["buy_down_size"]
    return pd.concat([up_frame, down_frame], ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    frame, runs = build_m1(Path(args.source_root))
    frame, features = add_features(frame)
    frame["p_up"] = expanding_probabilities(frame, runs, features)
    frame["p_down"] = 1.0 - frame["p_up"]
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    grid_rows: list[dict[str, object]] = []
    candidate_events: dict[str, pd.DataFrame] = {}
    for threshold in GRID_THRESHOLDS:
        for price_cap in GRID_PRICE_CAPS:
            for spread_cap in GRID_SPREAD_CAPS:
                label = f"p{threshold:.2f}_price{price_cap:.2f}_spread{spread_cap:.2f}"
                events = make_events(frame, threshold, price_cap, spread_cap)
                candidate_events[label] = events
                selection, _ = replay(events, selection_runs, 0.005)
                holdout, _ = replay(events, holdout_runs, 0.005)
                cursor = 0
                block_cash: list[float] = []
                for size in BLOCK_SIZES:
                    names = test_runs[cursor:cursor + size]
                    cursor += size
                    block_cash.append(replay(events, names, 0.005)[0]["ending_cash"])
                grid_rows.append({
                    "label": label, "threshold": threshold, "price_cap": price_cap,
                    "spread_cap": spread_cap, "signals": len(events),
                    "selection_trades": selection["trades"], "selection_cash": selection["ending_cash"],
                    "holdout_trades": holdout["trades"], "holdout_cash": holdout["ending_cash"],
                    "min_block_cash": min(block_cash), "block_cash": ",".join(f"{value:.6f}" for value in block_cash),
                })
    grid = pd.DataFrame(grid_rows)
    eligible = grid[grid["selection_trades"].ge(5)]
    selected_label = str(eligible.sort_values(["selection_cash", "selection_trades"], ascending=[False, False]).iloc[0]["label"]) if not eligible.empty else "none"
    selected = candidate_events.get(selected_label, pd.DataFrame())
    selected_summary: list[dict[str, object]] = []
    for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
        for slippage in (0.005, 0.01):
            metrics, trades = replay(selected, names, slippage)
            selected_summary.append({"split": split, "slippage": slippage, **metrics})
            if not trades.empty:
                trades.to_csv(output / f"selected_{split}_{slippage:.4f}_trades.csv", index=False)
    block_rows: list[dict[str, object]] = []
    cursor = 0
    for block, size in enumerate(BLOCK_SIZES, 1):
        names = test_runs[cursor:cursor + size]
        cursor += size
        for slippage in (0.005, 0.01):
            metrics, _ = replay(selected, names, slippage)
            block_rows.append({"block": block, "run_count": size, "slippage": slippage, **metrics, "gate": metrics["ending_cash"] >= 5.0 and metrics["net_pnl"] >= 0.0})
    grid.to_csv(output / "model_grid.csv", index=False)
    pd.DataFrame(selected_summary).to_csv(output / "selected_summary.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "selected_blocks.csv", index=False)
    result = {
        "format": "old-data-causal-expanding-logistic-small-cash-v1",
        "source_root": str(args.source_root), "complete_runs": len(runs), "usable_m1_markets": len(frame),
        "target_visible_at_decision_required": True,
        "model": {"kind": "expanding logistic", "training_runs_before_each_test_run": True, "feature_count": len(features)},
        "selection": {"runs": len(selection_runs), "eligible_min_selection_trades": 5, "selected_label": selected_label},
        "holdout_runs": len(holdout_runs), "test_runs": len(test_runs),
        "protocol": {"initial_cash": 10.0, "min_cost": 5.0, "fee_rate": 0.07, "fee_exponent": 1.0, "settlement_plus_release_seconds": SETTLEMENT_PLUS_RELEASE, "slippages": [0.005, 0.01], "fill_ratio": FILL_RATIO, "fraction": 0.04},
        "promotion_rule": "all five blocks must end >= $5 with nonnegative PnL at both slippages and holdout must contain at least 10 trades",
        "new_data_collected": False, "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "selected_summary": selected_summary, "selected_blocks": block_rows,
    }
    (output / "meta.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
