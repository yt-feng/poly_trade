"""Causal spot/target fair-value challenger on the clean old snapshot.

The model follows the public maker-family idea of using spot versus the
market target, time to expiry and volatility, but it is evaluated as a
taker-cost paper upper bound.  The old archive has no maker queue or fill
receipts, so this script cannot establish a maker fill or a Canary.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import all_monthly_clob_systematic_research_v2 as base
from causal_model_small_cash_replay import BLOCK_SIZES, replay

SECONDS_YEAR = 365.25 * 24.0 * 3600.0
SLIPPAGE = 0.005
FALLBACK_VOLS = (0.45, 0.60, 0.90)
VOL_MODES = ("realized", "fixed_0.45", "fixed_0.60", "fixed_0.90")
EDGE_BUFFERS = (0.00, 0.02, 0.04, 0.06)
PRICE_CAPS = (0.50, 0.55, 0.60)
SPREAD_CAPS = (0.04, 0.06)


def _first_non_null(series: pd.Series) -> float:
    values = series.dropna()
    return float(values.iloc[0]) if not values.empty else math.nan


def _last_non_null(series: pd.Series) -> float:
    values = series.dropna()
    return float(values.iloc[-1]) if not values.empty else math.nan


def _close_timestamp(slug: str) -> pd.Timestamp:
    try:
        return pd.to_datetime(int(str(slug).rsplit("-", 1)[1]) + 300, unit="s", utc=True)
    except (TypeError, ValueError, IndexError):
        return pd.NaT


def _realized_vol(prefix: pd.DataFrame) -> float:
    spots = pd.to_numeric(prefix["final_price"], errors="coerce").dropna()
    spots = spots[spots > 0]
    if len(spots) < 3:
        return math.nan
    returns = np.log(spots).diff().dropna()
    if returns.empty:
        return math.nan
    times = pd.to_datetime(prefix.loc[spots.index, "ts_utc"], utc=True).sort_values()
    dt = times.diff().dt.total_seconds().dropna()
    dt = dt[(dt > 0) & np.isfinite(dt)]
    if dt.empty:
        return math.nan
    annualized = float(returns.std(ddof=1)) / math.sqrt(float(dt.median())) * math.sqrt(SECONDS_YEAR)
    if not math.isfinite(annualized):
        return math.nan
    return float(np.clip(annualized, 0.10, 3.00))


def build_causal_m1(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"].eq(maximum), "run_name"].astype(str).tolist()
    quotes = base.prepare_quotes(raw[raw["run_name"].astype(str).isin(runs)].copy())
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", sort=False):
        group = group.sort_values("ts_utc").reset_index(drop=True)
        first_ts = group["ts_utc"].min()
        prefix = group[group["ts_utc"].le(first_ts + pd.Timedelta(minutes=1))]
        if prefix.empty:
            continue
        snapshot = prefix.iloc[-1]
        target = _first_non_null(prefix["target_price"])
        final = _last_non_null(group["final_price"])
        close = _close_timestamp(str(snapshot["slug"]))
        spot = pd.to_numeric(snapshot.get("final_price"), errors="coerce")
        if not math.isfinite(target) or not math.isfinite(final) or not pd.notna(spot) or not pd.notna(close):
            continue
        tau = (close - snapshot["ts_utc"]).total_seconds()
        if tau <= 0 or target <= 0 or float(spot) <= 0:
            continue
        values: dict[str, object] = {
            "market_id": str(market_id),
            "run_name": str(snapshot["run_name"]),
            "first_quote_ts": first_ts,
            "decision_ts": snapshot["ts_utc"],
            "close_ts": close,
            "tau_seconds": float(tau),
            "spot": float(spot),
            "target": float(target),
            "outcome_up": float(final > target),
            "realized_vol": _realized_vol(prefix),
        }
        for col in (
            "buy_up_cents", "buy_down_cents", "buy_up_size", "buy_down_size",
            "spread_up_cents", "spread_down_cents",
        ):
            value = pd.to_numeric(snapshot.get(col), errors="coerce")
            values[col] = float(value) if pd.notna(value) else math.nan
        rows.append(values)
    frame = pd.DataFrame(rows).sort_values("decision_ts").reset_index(drop=True)
    if frame.empty:
        raise RuntimeError("NO_CAUSAL_FAIR_VALUE_MARKETS")
    return frame, runs


def normal_cdf(value: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(value / math.sqrt(2.0)))


def add_probabilities(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    out = frame.copy()
    if mode == "realized":
        vol = pd.to_numeric(out["realized_vol"], errors="coerce").fillna(FALLBACK_VOLS[1]).to_numpy(dtype=float)
    else:
        vol = np.full(len(out), float(mode.split("_")[-1]), dtype=float)
    tau = out["tau_seconds"].to_numpy(dtype=float)
    ratio = np.log(out["spot"].to_numpy(dtype=float) / out["target"].to_numpy(dtype=float))
    z = ratio / (vol * np.sqrt(tau / SECONDS_YEAR))
    out["p_up"] = np.clip(normal_cdf(z), 0.001, 0.999)
    out["p_down"] = 1.0 - out["p_up"]
    out["vol_used"] = vol
    return out


def make_events(frame: pd.DataFrame, price_cap: float, spread_cap: float, edge_buffer: float) -> pd.DataFrame:
    result: list[pd.DataFrame] = []
    for side in ("up", "down"):
        price_col = f"buy_{side}_cents"
        size_col = f"buy_{side}_size"
        spread_col = f"spread_{side}_cents"
        probability = frame[f"p_{side}"]
        price = frame[price_col] / 100.0
        fill = price + SLIPPAGE
        fee_per_share = 0.07 * fill * (1.0 - fill)
        edge = probability - fill - fee_per_share
        keep = (
            probability.notna() & price.gt(0) & price.le(price_cap)
            & frame[spread_col].le(spread_cap * 100.0)
            & edge.ge(edge_buffer)
        )
        part = frame.loc[keep, ["market_id", "run_name", "first_quote_ts", "outcome_up", size_col, price_col]].copy()
        part["side"] = side
        part["price"] = part[price_col] / 100.0
        part["size"] = part[size_col]
        result.append(part[["market_id", "run_name", "first_quote_ts", "outcome_up", "side", "price", "size"]])
    return pd.concat(result, ignore_index=True) if result else pd.DataFrame()


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    frame, runs = build_causal_m1(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    rows: list[dict[str, object]] = []
    for mode, edge, price_cap, spread_cap in itertools.product(
        VOL_MODES, EDGE_BUFFERS, PRICE_CAPS, SPREAD_CAPS
    ):
        scored = add_probabilities(frame, mode)
        events = make_events(scored, price_cap, spread_cap, edge)
        selection, _ = replay(events, selection_runs, SLIPPAGE)
        holdout, _ = replay(events, holdout_runs, SLIPPAGE)
        cursor = 0
        block_cash: list[float] = []
        for size in BLOCK_SIZES:
            names = test_runs[cursor : cursor + size]
            cursor += size
            block_cash.append(replay(events, names, SLIPPAGE)[0]["ending_cash"])
        rows.append({
            "vol_mode": mode, "edge_buffer": edge, "price_cap": price_cap, "spread_cap": spread_cap,
            "signals": len(events), "selection_trades": selection["trades"], "selection_cash": selection["ending_cash"],
            "holdout_trades": holdout["trades"], "holdout_cash": holdout["ending_cash"],
            "min_block_cash": min(block_cash), "block_cash": ",".join(f"{v:.6f}" for v in block_cash),
        })
    grid = pd.DataFrame(rows)
    eligible = grid[grid["selection_trades"].ge(10)].sort_values(["min_block_cash", "selection_cash"], ascending=[False, False])
    output.mkdir(parents=True, exist_ok=True)
    grid.to_csv(output / "grid.csv", index=False)
    summary = {
        "format": "old-clean-h89-gbm-fair-value-v1", "source_root": str(source_root), "complete_runs": len(runs),
        "usable_m1_markets": len(frame), "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "vol_modes": list(VOL_MODES), "edge_buffers": list(EDGE_BUFFERS), "price_caps": list(PRICE_CAPS),
        "spread_caps": list(SPREAD_CAPS), "grid_cells": len(grid), "selection_eligible_cells": len(eligible),
        "realized_vol_nonnull": int(frame["realized_vol"].notna().sum()), "realized_vol_fallback": int(frame["realized_vol"].isna().sum()),
        "best_eligible": eligible.head(1).to_dict(orient="records"), "same_ledger": True, "required_fills": 10,
        "initial_cash": 10.0, "minimum_order": 5.0, "fee_rate": 0.07, "slippage": SLIPPAGE,
        "paper_only": True, "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()
