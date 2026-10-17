"""Chain the frozen persisted-entry candidate across chronological test runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from v1_persisted_4m_entry_walk_forward import apply_persisted_entry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--fee", type=float, default=0.07)
    parser.add_argument("--fee-mode", choices=("flat_per_share", "polymarket_taker"), default="polymarket_taker")
    parser.add_argument("--fee-exponent", type=float, default=1.0)
    parser.add_argument("--min-cost-policy", choices=("reject", "raise_to_min"), default="raise_to_min")
    args = parser.parse_args()
    study.FEE = float(args.fee)
    features, _, runs = study.prepare_data(Path(args.source_root))
    folds = study.make_run_folds(runs)
    config = study.ExecutionConfig()
    rows = []
    for strategy in ("v1_conservative_mix", "v1_adaptive_mix"):
        bankroll = float(args.bankroll0)
        for fold in folds:
            frame = features[features["run_name"] == fold["test_run"]].copy()
            filtered = apply_persisted_entry(frame, strategy)
            _, metrics = study.simulate(
                filtered,
                strategy,
                config,
                bankroll,
                args.min_cost,
                capture_logs=False,
                fee_mode=args.fee_mode,
                fee_rate=args.fee,
                fee_exponent=args.fee_exponent,
                min_cost_policy=args.min_cost_policy,
            )
            rows.append({
                "strategy": strategy,
                "fold_id": fold["fold_id"],
                "test_run": fold["test_run"],
                "starting_bankroll": bankroll,
                "ending_bankroll": metrics["ending_bankroll"],
                "total_return": metrics["total_return"],
                "trades": metrics["trades"],
                "max_drawdown": metrics["max_drawdown"],
                "win_rate": metrics["win_rate"],
            })
            bankroll = float(metrics["ending_bankroll"])
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    summary = (
        result.groupby("strategy", as_index=False)
        .agg(
            test_folds=("fold_id", "nunique"),
            ending_bankroll=("ending_bankroll", "last"),
            total_trades=("trades", "sum"),
            positive_folds=("total_return", lambda s: int((s > 0).sum())),
            zero_trade_folds=("trades", lambda s: int((s == 0).sum())),
            minimum_bankroll=("ending_bankroll", "min"),
            maximum_drawdown=("max_drawdown", "max"),
        )
    )
    summary_path = out.with_name(out.stem + "_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(json.dumps({
        "source_complete_runs": len(runs),
        "fold_count": len(folds),
        "bankroll0": args.bankroll0,
        "min_cost": args.min_cost,
        "min_cost_policy": args.min_cost_policy,
        "fee": args.fee,
        "fee_mode": args.fee_mode,
        "output": str(out),
        "summary": str(summary_path),
    }, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
