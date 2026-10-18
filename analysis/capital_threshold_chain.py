"""Measure continuous cash-chain capacity for a frozen signal at several bankrolls."""
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
    parser.add_argument("--bankrolls", default="10,15,20,25,50,100")
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--fee", type=float, default=0.07)
    args = parser.parse_args()
    bankrolls = [float(x) for x in args.bankrolls.split(",") if x.strip()]
    features, _, runs = study.prepare_data(Path(args.source_root))
    folds = study.make_run_folds(runs)
    config = study.ExecutionConfig()
    rows = []
    for start in bankrolls:
        for strategy in ("v1_conservative_mix", "v1_adaptive_mix"):
            bankroll = start
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
                    fee_mode="polymarket_taker",
                    fee_rate=args.fee,
                    fee_exponent=1.0,
                    min_cost_policy="raise_to_min",
                )
                rows.append({
                    "starting_bankroll": start,
                    "strategy": strategy,
                    "fold_id": fold["fold_id"],
                    "test_run": fold["test_run"],
                    "cash_before": bankroll,
                    "cash_after": metrics["ending_bankroll"],
                    "return": metrics["total_return"],
                    "trades": metrics["trades"],
                    "max_drawdown": metrics["max_drawdown"],
                })
                bankroll = float(metrics["ending_bankroll"])
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    summary = result.groupby(["starting_bankroll", "strategy"], as_index=False).agg(
        ending_bankroll=("cash_after", "last"),
        total_trades=("trades", "sum"),
        positive_folds=("return", lambda s: int((s > 0).sum())),
        zero_trade_folds=("trades", lambda s: int((s == 0).sum())),
        minimum_cash=("cash_after", "min"),
        maximum_drawdown=("max_drawdown", "max"),
    )
    summary_path = out.with_name(out.stem + "_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(json.dumps({"complete_runs": len(runs), "fold_count": len(folds), "bankrolls": bankrolls, "min_cost": args.min_cost, "fee": args.fee}, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
