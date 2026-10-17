"""Run frozen v1 session-gated candidates through the corrected fill model.

The two candidates are evaluated on the existing monthly_runs source only. The
execution configuration is fixed before the walk-forward run so that the test
folds do not select their own delay or liquidity thresholds.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
from quantml_candidate_execution_walk_forward import attach_execution_snapshots

FEE = 0.01
FILL_RATIO = 0.25
QUOTA_WINDOW_EVENTS = 144
MAX_TRADES_PER_WINDOW = 10
COOLDOWN_AFTER_LOSSES = 2
COOLDOWN_EVENTS = 6
STRATEGIES = ("v1_conservative_mix", "v1_adaptive_mix", "v1_no_london_milddrop")


@dataclass(frozen=True)
class ExecutionConfig:
    delay_seconds: int = 4
    max_spread: float = 0.04
    max_overround: float = 0.03
    min_depth: float = 100.0
    max_adverse_slippage: float = 0.015
    fixed_slippage: float = 0.0025


def as_num(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)


def complete_runs(coverage: pd.DataFrame) -> List[str]:
    if coverage.empty:
        return []
    maximum = int(coverage["file_count"].max())
    return coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()


def make_run_folds(runs: List[str], minimum_train_runs: int = 3) -> List[Dict[str, object]]:
    return [
        {
            "fold_id": fold_id,
            "train_runs": runs[:i],
            "validation_run": runs[i],
            "test_run": runs[i + 1],
        }
        for fold_id, i in enumerate(range(minimum_train_runs, len(runs) - 1), start=1)
    ]


def prepare_data(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    runs = complete_runs(coverage)
    if len(runs) < 5:
        raise RuntimeError(f"Need at least five complete runs, found {len(runs)}")
    quotes = base.prepare_quotes(raw[raw["run_name"].astype(str).isin(runs)].copy())
    features = base.build_features(quotes)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    if features.empty:
        raise RuntimeError("No usable complete-run features")

    for strategy in STRATEGIES:
        choices = features.apply(lambda row: choose_trade(row, strategy), axis=1)
        features[f"{strategy}_side"] = choices.map(lambda item: item[0])
        features[f"{strategy}_minute"] = choices.map(lambda item: item[1]).astype(int)
        features[f"{strategy}_fraction"] = choices.map(lambda item: item[2]).fillna(0.0).astype(float)
        features[f"{strategy}_component"] = choices.map(lambda item: item[3])
    coverage["is_complete_run"] = coverage["run_name"].astype(str).isin(runs)
    return features, coverage, runs


def choose_trade(row: pd.Series, strategy: str) -> Tuple[str, int, float, str]:
    if strategy == "v1_no_london_milddrop":
        side, minute, fraction, component = v1.choose_trade(row, "v1_conservative_mix")
        if component == "london_milddrop":
            return "skip", -1, np.nan, "disabled_london_milddrop"
        return side, minute, fraction, component
    return v1.choose_trade(row, strategy)


def window_metrics(event_returns: np.ndarray, trade_flags: np.ndarray) -> Dict[str, float]:
    if len(event_returns) < QUOTA_WINDOW_EVENTS:
        return {
            "worst_144_return": np.nan,
            "median_144_return": np.nan,
            "positive_144_rate": np.nan,
            "active_144_rate": np.nan,
        }
    returns, active = [], []
    for i in range(len(event_returns) - QUOTA_WINDOW_EVENTS + 1):
        returns.append(float(np.prod(1.0 + event_returns[i : i + QUOTA_WINDOW_EVENTS]) - 1.0))
        active.append(float(np.sum(trade_flags[i : i + QUOTA_WINDOW_EVENTS]) > 0))
    return {
        "worst_144_return": float(np.min(returns)),
        "median_144_return": float(np.median(returns)),
        "positive_144_rate": float(np.mean(np.asarray(returns) > 0)),
        "active_144_rate": float(np.mean(np.asarray(active) > 0)),
    }


def trade_fee_usd(
    shares: float,
    price: float,
    fee_mode: str = "flat_per_share",
    fee_rate: float = FEE,
    fee_exponent: float = 1.0,
) -> float:
    """Return the fee for one taker trade under the selected fee contract."""
    if not math.isfinite(float(shares)) or float(shares) < 0:
        raise ValueError("shares must be finite and non-negative")
    if not math.isfinite(float(price)) or not 0.0 < float(price) < 1.0:
        raise ValueError("price must be finite and between zero and one")
    if not math.isfinite(float(fee_rate)) or float(fee_rate) < 0:
        raise ValueError("fee_rate must be finite and non-negative")
    if not math.isfinite(float(fee_exponent)) or float(fee_exponent) <= 0:
        raise ValueError("fee_exponent must be finite and positive")
    if fee_mode == "flat_per_share":
        return float(shares) * float(fee_rate)
    if fee_mode == "polymarket_taker":
        # Polymarket crypto taker schedule: C * rate * p^exponent * (1-p).
        return round(float(shares) * float(fee_rate) * (float(price) ** float(fee_exponent)) * (1.0 - float(price)), 5)
    raise ValueError(f"unsupported fee_mode: {fee_mode}")


def simulate(
    df: pd.DataFrame,
    strategy: str,
    config: ExecutionConfig,
    bankroll0: float,
    min_cost: float,
    capture_logs: bool = False,
    fee_mode: str = "flat_per_share",
    fee_rate: float | None = None,
    fee_exponent: float = 1.0,
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    if fee_mode not in {"flat_per_share", "polymarket_taker"}:
        raise ValueError(f"unsupported fee_mode: {fee_mode}")
    if fee_rate is None:
        fee_rate = FEE
    if not math.isfinite(float(fee_rate)) or float(fee_rate) < 0:
        raise ValueError("fee_rate must be finite and non-negative")
    if not math.isfinite(float(fee_exponent)) or float(fee_exponent) <= 0:
        raise ValueError("fee_exponent must be finite and positive")
    d = df.sort_values("first_quote_ts").reset_index(drop=True)
    side = d[f"{strategy}_side"].fillna("skip").astype(str).to_numpy()
    minute = d[f"{strategy}_minute"].to_numpy(dtype=int)
    fraction = d[f"{strategy}_fraction"].to_numpy(dtype=float)
    signal = side != "skip"
    quote_price = np.full(len(d), np.nan)
    signal_price = np.full(len(d), np.nan)
    quote_size = np.full(len(d), np.nan)
    quote_spread = np.full(len(d), np.nan)
    quote_overround = np.full(len(d), np.nan)
    adverse = np.full(len(d), np.nan)
    for side_name in ("buy_up", "buy_down"):
        side_mask = signal & (side == side_name)
        for entry_minute in (1, 2, 4):
            rows = side_mask & (minute == entry_minute)
            prefix = f"exec_{side_name}_{entry_minute}m_d{config.delay_seconds}"
            quote_price[rows] = as_num(d.loc[rows, f"{prefix}_quote_price"]).to_numpy()
            signal_price[rows] = as_num(d.loc[rows, f"{prefix}_signal_price"]).to_numpy()
            quote_size[rows] = as_num(d.loc[rows, f"{prefix}_quote_size"]).to_numpy()
            quote_spread[rows] = as_num(d.loc[rows, f"{prefix}_quote_spread"]).to_numpy()
            quote_overround[rows] = as_num(d.loc[rows, f"{prefix}_quote_overround"]).to_numpy()
            adverse[rows] = as_num(d.loc[rows, f"{prefix}_adverse_slippage"]).to_numpy()

    fill_price = quote_price + config.fixed_slippage
    outcome = as_num(d["outcome_up"]).to_numpy(dtype=float)
    eligible = (
        signal
        & np.isfinite(signal_price)
        & np.isfinite(quote_price)
        & np.isfinite(fill_price)
        & np.isfinite(quote_size)
        & np.isfinite(quote_spread)
        & np.isfinite(quote_overround)
        & np.isfinite(adverse)
        & np.isfinite(outcome)
        & (quote_price >= 0.05)
        & (quote_price <= 0.95)
        & (fill_price < 0.98)
        & (quote_size >= config.min_depth)
        & (quote_spread <= config.max_spread)
        & (quote_overround <= config.max_overround)
        & (adverse <= config.max_adverse_slippage)
    )

    bankroll = float(bankroll0)
    peak = bankroll
    max_drawdown = 0.0
    event_returns = np.zeros(len(d), dtype=float)
    trade_flags = np.zeros(len(d), dtype=int)
    logs: List[Dict[str, object]] = []
    recent_trades: List[int] = []
    loss_streak = 0
    cooldown = 0
    for i in np.flatnonzero(signal):
        recent_trades = [index for index in recent_trades if index > i - QUOTA_WINDOW_EVENTS]
        if cooldown > 0:
            cooldown -= 1
            continue
        if len(recent_trades) >= MAX_TRADES_PER_WINDOW or not eligible[i] or bankroll <= 0:
            continue
        capacity = float(quote_size[i]) * FILL_RATIO * float(fill_price[i])
        cost = min(bankroll * max(fraction[i], 0.0), bankroll, capacity)
        if not math.isfinite(cost) or cost < min_cost or fill_price[i] <= 0:
            continue
        shares = cost / fill_price[i]
        gross_pnl = shares * ((outcome[i] if side[i] == "buy_up" else 1.0 - outcome[i]) - fill_price[i])
        fee_usd = trade_fee_usd(shares, fill_price[i], fee_mode, float(fee_rate), float(fee_exponent))
        pnl = gross_pnl - fee_usd
        before = bankroll
        bankroll += pnl
        event_return = pnl / before if before > 0 else 0.0
        event_returns[i] = event_return
        trade_flags[i] = 1
        recent_trades.append(i)
        peak = max(peak, bankroll)
        max_drawdown = max(max_drawdown, (peak - bankroll) / peak if peak > 0 else 0.0)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= COOLDOWN_AFTER_LOSSES:
                cooldown = COOLDOWN_EVENTS
                loss_streak = 0
        else:
            loss_streak = 0
        if capture_logs:
            logs.append(
                {
                    "strategy": strategy,
                    "first_quote_ts": d.at[i, "first_quote_ts"],
                    "run_name": d.at[i, "run_name"],
                    "market_id": d.at[i, "market_id"],
                    "component": d.at[i, f"{strategy}_component"],
                    "side": side[i],
                    "entry_minute": int(minute[i]),
                    "delay_seconds": config.delay_seconds,
                    "fraction": fraction[i],
                    "signal_price": signal_price[i],
                    "quote_price": quote_price[i],
                    "fill_price": fill_price[i],
                    "quote_size": quote_size[i],
                    "quote_spread": quote_spread[i],
                    "quote_overround": quote_overround[i],
                    "adverse_slippage": adverse[i],
                    "target_cost": before * fraction[i],
                    "filled_cost": cost,
                    "fee_usd": fee_usd,
                    "pnl_usd": pnl,
                    "event_return": event_return,
                    "bankroll_after": bankroll,
                }
            )

    pnl_series = pd.Series([row["pnl_usd"] for row in logs], dtype=float) if capture_logs else pd.Series(bankroll0 * event_returns[trade_flags == 1], dtype=float)
    wins = float(pnl_series[pnl_series > 0].sum()) if not pnl_series.empty else 0.0
    losses = float(pnl_series[pnl_series < 0].sum()) if not pnl_series.empty else 0.0
    metrics: Dict[str, object] = {
        "strategy": strategy,
        "signals": int(signal.sum()),
        "eligible_fills": int(eligible.sum()),
        "trades": int(trade_flags.sum()),
        "ending_bankroll": bankroll,
        "total_return": bankroll / bankroll0 - 1.0,
        "max_drawdown": max_drawdown,
        "win_rate": float((pnl_series > 0).mean()) if not pnl_series.empty else np.nan,
        "profit_factor": float(wins / abs(losses)) if losses else np.nan,
        "fill_rate_vs_signals": float(eligible.sum() / signal.sum()) if signal.any() else np.nan,
        "trade_rate_vs_signals": float(trade_flags.sum() / signal.sum()) if signal.any() else np.nan,
        "fee_mode": fee_mode,
        "fee_rate": float(fee_rate),
        "fee_exponent": float(fee_exponent),
        **asdict(config),
        **window_metrics(event_returns, trade_flags),
    }
    return pd.DataFrame(logs), metrics


def run_walk_forward(features: pd.DataFrame, folds: List[Dict[str, object]], bankroll0: float, min_cost: float):
    rows: List[Dict[str, object]] = []
    logs: List[pd.DataFrame] = []
    config = ExecutionConfig()
    for fold in folds:
        train_runs = list(fold["train_runs"])
        splits = [
            ("train", features[features["run_name"].isin(train_runs)], "+".join(train_runs)),
            ("validation", features[features["run_name"] == fold["validation_run"]], str(fold["validation_run"])),
            ("test", features[features["run_name"] == fold["test_run"]], str(fold["test_run"])),
        ]
        for strategy in STRATEGIES:
            for split_name, frame, run_label in splits:
                trade_log, metrics = simulate(frame, strategy, config, bankroll0, min_cost, capture_logs=True)
                metrics.update({"split": split_name, "fold_id": fold["fold_id"], "run_label": run_label})
                rows.append(metrics)
                if not trade_log.empty:
                    trade_log["split"] = split_name
                    trade_log["fold_id"] = fold["fold_id"]
                    trade_log["run_label"] = run_label
                    logs.append(trade_log)
    return pd.DataFrame(rows), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def aggregate(metrics: pd.DataFrame) -> pd.DataFrame:
    test = metrics[metrics["split"] == "test"]
    return (
        test.groupby("strategy", as_index=False)
        .agg(
            folds_tested=("fold_id", "nunique"),
            avg_test_return=("total_return", "mean"),
            median_test_return=("total_return", "median"),
            min_test_return=("total_return", "min"),
            pct_positive_test=("total_return", lambda s: float((s > 0).mean())),
            avg_test_drawdown=("max_drawdown", "mean"),
            max_test_drawdown=("max_drawdown", "max"),
            avg_test_trades=("trades", "mean"),
            avg_test_fill_rate=("fill_rate_vs_signals", "mean"),
            avg_test_win_rate=("win_rate", "mean"),
        )
        .sort_values("avg_test_return", ascending=False)
        .reset_index(drop=True)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=0.10)
    args = parser.parse_args()
    if args.bankroll0 <= 0 or args.min_cost < 0:
        raise SystemExit("bankroll0 must be positive and min-cost must be non-negative")
    features, coverage, runs = prepare_data(Path(args.source_root))
    folds = make_run_folds(runs)
    metrics, logs = run_walk_forward(features, folds, args.bankroll0, args.min_cost)
    summary = aggregate(metrics)
    out = Path(args.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(out / "coverage_complete_runs.csv", index=False)
    pd.DataFrame(folds).to_csv(out / "walk_forward_folds.csv", index=False)
    metrics.to_csv(out / "strategy_split_metrics.csv", index=False)
    logs.to_csv(out / "trade_logs.csv", index=False)
    summary.to_csv(out / "strategy_summary.csv", index=False)
    meta = {
        "complete_runs": len(runs),
        "fold_count": len(folds),
        "feature_rows": len(features),
        "trade_log_rows": len(logs),
        "bankroll0": args.bankroll0,
        "min_cost": args.min_cost,
        "execution_config": asdict(ExecutionConfig()),
        "strategies": list(STRATEGIES),
        "fee": FEE,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
