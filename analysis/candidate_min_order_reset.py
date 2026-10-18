"""Reset-fold comparison for promising candidates under the documented floor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import CANDIDATES, prepare


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidates", default="classic_breakout_up,micro_book_consensus_up")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--fee", type=float, default=0.07)
    args = parser.parse_args()
    selected = [x.strip() for x in args.candidates.split(",") if x.strip()]
    unknown = sorted(set(selected) - set(CANDIDATES))
    if unknown:
        raise SystemExit(f"unknown candidates: {unknown}")
    features, runs = prepare(Path(args.source_root), selected)
    folds = study.make_run_folds(runs)
    config = study.ExecutionConfig()
    rows = []
    for strategy in selected:
        for fold in folds:
            frame = features[features["run_name"] == fold["test_run"]].copy()
            _, metrics = study.simulate(
                frame,
                strategy,
                config,
                args.bankroll0,
                args.min_cost,
                capture_logs=False,
                fee_mode="polymarket_taker",
                fee_rate=args.fee,
                min_order_mode="notional",
                fee_exponent=1.0,
                min_cost_policy="raise_to_min",
            )
            rows.append({"strategy": strategy, "fold_id": fold["fold_id"], "test_run": fold["test_run"], **metrics})
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    summary = result.groupby("strategy", as_index=False).agg(
        folds_tested=("fold_id", "nunique"),
        avg_test_return=("total_return", "mean"),
        median_test_return=("total_return", "median"),
        min_test_return=("total_return", "min"),
        pct_positive_test=("total_return", lambda s: float((s > 0).mean())),
        max_test_drawdown=("max_drawdown", "max"),
        avg_test_trades=("trades", "mean"),
        total_test_trades=("trades", "sum"),
        zero_trade_folds=("trades", lambda s: int((s == 0).sum())),
        avg_test_win_rate=("win_rate", "mean"),
    )
    summary_path = out.with_name(out.stem + "_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(json.dumps({"complete_runs": len(runs), "fold_count": len(folds), "bankroll0": args.bankroll0, "min_cost": args.min_cost, "fee": args.fee}, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
