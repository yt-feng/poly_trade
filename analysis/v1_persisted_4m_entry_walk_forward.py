"""Evaluate a frozen delayed-entry rule using existing four-minute snapshots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import pandas as pd

import v1_execution_walk_forward as study

STRATEGIES = ("v1_conservative_mix", "v1_adaptive_mix")


def apply_persisted_entry(frame: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = frame.copy()
    side = out[f"{strategy}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(out[f"{strategy}_minute"], errors="coerce").fillna(-1).astype(int)
    move4 = pd.to_numeric(out["btc_move_4m"], errors="coerce")
    # Existing v1 buy-down signals are two-minute entries.  Wait for four
    # minutes and re-use the same side/fraction only when the move remains down.
    down_two_min = (side == "buy_down") & (minute == 2)
    keep_down = down_two_min & (move4 < 0.0)
    drop = down_two_min & ~keep_down
    out.loc[keep_down, f"{strategy}_minute"] = 4
    out.loc[drop, f"{strategy}_side"] = "skip"
    out.loc[drop, f"{strategy}_minute"] = -1
    out.loc[drop, f"{strategy}_fraction"] = 0.0
    return out


def run(features: pd.DataFrame, folds: List[Dict[str, object]], bankroll0: float, min_cost: float):
    config = study.ExecutionConfig()
    rows: List[Dict[str, object]] = []
    logs: List[pd.DataFrame] = []
    for fold in folds:
        splits = [
            ("train", features[features["run_name"].isin(fold["train_runs"])], "+".join(fold["train_runs"])),
            ("validation", features[features["run_name"] == fold["validation_run"]], str(fold["validation_run"])),
            ("test", features[features["run_name"] == fold["test_run"]], str(fold["test_run"])),
        ]
        for strategy in STRATEGIES:
            for split_name, frame, run_label in splits:
                filtered = apply_persisted_entry(frame, strategy)
                trade_log, metrics = study.simulate(
                    filtered, strategy, config, bankroll0, min_cost, capture_logs=True
                )
                metrics.update({
                    "candidate": f"{strategy}__persisted_4m_entry",
                    "split": split_name,
                    "fold_id": fold["fold_id"],
                    "run_label": run_label,
                })
                rows.append(metrics)
                if not trade_log.empty:
                    trade_log["candidate"] = f"{strategy}__persisted_4m_entry"
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
            total_test_trades=("trades", "sum"),
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
    parser.add_argument("--fee", type=float, default=study.FEE)
    args = parser.parse_args()
    if args.bankroll0 <= 0 or args.min_cost < 0 or args.fee < 0:
        raise SystemExit("bankroll0 must be positive, min-cost and fee must be non-negative")
    study.FEE = float(args.fee)
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
        "rule": "two-minute buy_down signals are entered at four minutes only when btc_move_4m < 0; otherwise skipped",
        "fee": args.fee,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
