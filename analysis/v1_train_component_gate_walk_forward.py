"""Train-only component gating for the frozen v1 execution candidates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Set

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study

BASE_STRATEGIES = ("v1_conservative_mix", "v1_adaptive_mix")


def gated_frame(df: pd.DataFrame, strategy: str, active: Set[str]) -> pd.DataFrame:
    out = df.copy()
    keep = out[f"{strategy}_component"].isin(active)
    out.loc[~keep, f"{strategy}_side"] = "skip"
    out.loc[~keep, f"{strategy}_minute"] = -1
    out.loc[~keep, f"{strategy}_fraction"] = 0.0
    return out


def component_training_returns(
    train: pd.DataFrame,
    strategy: str,
    config: study.ExecutionConfig,
    bankroll0: float,
    min_cost: float,
) -> Dict[str, float]:
    components = sorted(
        set(train[f"{strategy}_component"].dropna().astype(str)) - {"none", "disabled_london_milddrop"}
    )
    result: Dict[str, float] = {}
    for component in components:
        _, metrics = study.simulate(
            gated_frame(train, strategy, {component}),
            strategy,
            config,
            bankroll0,
            min_cost,
            capture_logs=False,
        )
        result[component] = float(metrics["total_return"])
    return result


def run(
    features: pd.DataFrame,
    folds: List[Dict[str, object]],
    bankroll0: float,
    min_cost: float,
):
    config = study.ExecutionConfig()
    metrics_rows: List[Dict[str, object]] = []
    decisions: List[Dict[str, object]] = []
    logs: List[pd.DataFrame] = []
    for fold in folds:
        train = features[features["run_name"].isin(fold["train_runs"])].copy()
        validation = features[features["run_name"] == fold["validation_run"]].copy()
        test = features[features["run_name"] == fold["test_run"]].copy()
        for strategy in BASE_STRATEGIES:
            component_returns = component_training_returns(train, strategy, config, bankroll0, min_cost)
            active = {component for component, value in component_returns.items() if value > 0.0}
            decisions.append(
                {
                    "fold_id": fold["fold_id"],
                    "strategy": strategy,
                    "active_components": ",".join(sorted(active)),
                    "component_training_returns": json.dumps(component_returns, sort_keys=True),
                }
            )
            for split_name, frame, run_label in (
                ("train", train, "+".join(fold["train_runs"])),
                ("validation", validation, str(fold["validation_run"])),
                ("test", test, str(fold["test_run"])),
            ):
                gated = gated_frame(frame, strategy, active)
                trade_log, metrics = study.simulate(
                    gated,
                    strategy,
                    config,
                    bankroll0,
                    min_cost,
                    capture_logs=True,
                )
                metrics.update(
                    {
                        "split": split_name,
                        "fold_id": fold["fold_id"],
                        "run_label": run_label,
                        "active_components": ",".join(sorted(active)),
                    }
                )
                metrics_rows.append(metrics)
                if not trade_log.empty:
                    trade_log["split"] = split_name
                    trade_log["fold_id"] = fold["fold_id"]
                    trade_log["run_label"] = run_label
                    trade_log["active_components"] = ",".join(sorted(active))
                    logs.append(trade_log)
    return pd.DataFrame(metrics_rows), pd.DataFrame(decisions), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


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
    features, coverage, runs = study.prepare_data(Path(args.source_root))
    folds = study.make_run_folds(runs)
    metrics, decisions, logs = run(features, folds, args.bankroll0, args.min_cost)
    summary = aggregate(metrics)
    out = Path(args.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(out / "coverage_complete_runs.csv", index=False)
    pd.DataFrame(folds).to_csv(out / "walk_forward_folds.csv", index=False)
    decisions.to_csv(out / "gate_decisions.csv", index=False)
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
        "base_strategies": list(BASE_STRATEGIES),
        "gate_rule": "component training total_return > 0",
        "fee": study.FEE,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
