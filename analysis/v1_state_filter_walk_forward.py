"""Evaluate frozen state filters on the existing v1 execution candidates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study

STRATEGIES = ("v1_conservative_mix", "v1_adaptive_mix")
FILTERS = ("path_efficiency_ge_050", "entry_price_midrange_020_080")


def entry_price(df: pd.DataFrame, strategy: str) -> pd.Series:
    side = df[f"{strategy}_side"].fillna("skip")
    minute = df[f"{strategy}_minute"]
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for side_name in ("buy_up", "buy_down"):
        for minute_value in (1, 2, 4):
            rows = side.eq(side_name) & minute.eq(minute_value)
            result.loc[rows] = pd.to_numeric(
                df.loc[rows, f"{side_name}_price_{minute_value}m"], errors="coerce"
            )
    return result


def state_mask(df: pd.DataFrame, strategy: str, filter_name: str) -> pd.Series:
    if filter_name == "path_efficiency_ge_050":
        return pd.to_numeric(df["path_efficiency_first2m"], errors="coerce").ge(0.50)
    if filter_name == "entry_price_midrange_020_080":
        price = entry_price(df, strategy)
        return price.ge(0.20) & price.le(0.80)
    raise ValueError(f"unknown filter: {filter_name}")


def filtered_frame(df: pd.DataFrame, strategy: str, filter_name: str) -> pd.DataFrame:
    out = df.copy()
    keep = state_mask(out, strategy, filter_name)
    out.loc[~keep, f"{strategy}_side"] = "skip"
    out.loc[~keep, f"{strategy}_minute"] = -1
    out.loc[~keep, f"{strategy}_fraction"] = 0.0
    return out


def make_folds(features: pd.DataFrame, runs: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    return features, pd.DataFrame(study.make_run_folds(runs))


def run(features: pd.DataFrame, folds: List[Dict[str, object]], bankroll0: float, min_cost: float):
    config = study.ExecutionConfig()
    rows: List[Dict[str, object]] = []
    logs: List[pd.DataFrame] = []
    for fold in folds:
        train = features[features["run_name"].isin(fold["train_runs"])].copy()
        validation = features[features["run_name"] == fold["validation_run"]].copy()
        test = features[features["run_name"] == fold["test_run"]].copy()
        for strategy in STRATEGIES:
            for filter_name in FILTERS:
                candidate = f"{strategy}__{filter_name}"
                for split_name, frame, run_label in (
                    ("train", train, "+".join(fold["train_runs"])),
                    ("validation", validation, str(fold["validation_run"])),
                    ("test", test, str(fold["test_run"])),
                ):
                    gated = filtered_frame(frame, strategy, filter_name)
                    trade_log, metrics = study.simulate(
                        gated, strategy, config, bankroll0, min_cost,
                        capture_logs=True, min_order_mode="notional",
                    )
                    metrics.update(
                        {
                            "candidate": candidate,
                            "filter_name": filter_name,
                            "split": split_name,
                            "fold_id": fold["fold_id"],
                            "run_label": run_label,
                            "state_pass_rate": float(state_mask(frame, strategy, filter_name).mean()),
                        }
                    )
                    rows.append(metrics)
                    if not trade_log.empty:
                        trade_log["candidate"] = candidate
                        trade_log["filter_name"] = filter_name
                        trade_log["split"] = split_name
                        trade_log["fold_id"] = fold["fold_id"]
                        trade_log["run_label"] = run_label
                        logs.append(trade_log)
    return pd.DataFrame(rows), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def aggregate(metrics: pd.DataFrame) -> pd.DataFrame:
    test = metrics[metrics["split"] == "test"]
    return (
        test.groupby("candidate", as_index=False)
        .agg(
            folds_tested=("fold_id", "nunique"),
            avg_test_return=("total_return", "mean"),
            median_test_return=("total_return", "median"),
            min_test_return=("total_return", "min"),
            pct_positive_test=("total_return", lambda s: float((s > 0).mean())),
            avg_test_drawdown=("max_drawdown", "mean"),
            max_test_drawdown=("max_drawdown", "max"),
            avg_test_trades=("trades", "mean"),
            avg_state_pass_rate=("state_pass_rate", "mean"),
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
    features, coverage, runs = study.prepare_data(Path(args.source_root))
    folds = study.make_run_folds(runs)
    metrics, logs = run(features, folds, args.bankroll0, args.min_cost)
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
        "execution_config": study.asdict(study.ExecutionConfig()),
        "filters": list(FILTERS),
        "fee": study.FEE,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
