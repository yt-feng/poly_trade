"""Validation-only candidate selection under the documented minimum order."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import CANDIDATES, prepare


def score(metrics: dict) -> float:
    # Predeclared validation rule: reward net return, penalize drawdown, and
    # leave no-trade validation at score zero rather than inventing edge.
    if int(metrics.get("trades", 0)) == 0:
        return 0.0
    return float(metrics["total_return"]) - 0.5 * float(metrics["max_drawdown"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--fee", type=float, default=0.07)
    parser.add_argument("--min-order-mode", choices=("notional", "shares", "both"), default="notional")
    parser.add_argument("--min-order-shares", type=float, default=5.0)
    parser.add_argument("--validation-window", type=int, choices=(1, 3), default=1)
    args = parser.parse_args()
    features, runs = prepare(Path(args.source_root), list(CANDIDATES))
    folds = study.make_run_folds(runs, validation_window=args.validation_window)
    config = study.ExecutionConfig()
    rows = []
    bankroll = float(args.bankroll0)
    for fold in folds:
        validation = features[features["run_name"].isin(fold["validation_runs"])].copy()
        test = features[features["run_name"] == fold["test_run"]].copy()
        val_metrics = []
        for candidate in CANDIDATES:
            _, metrics = study.simulate(
                validation,
                candidate,
                config,
                args.bankroll0,
                args.min_cost,
                capture_logs=False,
                fee_mode="polymarket_taker",
                fee_rate=args.fee,
                fee_exponent=1.0,
                min_cost_policy="raise_to_min",
                min_order_mode=args.min_order_mode,
                min_order_shares=args.min_order_shares,
            )
            val_metrics.append({"candidate": candidate, "validation_score": score(metrics), **metrics})
        val_df = pd.DataFrame(val_metrics).sort_values(["validation_score", "total_return", "trades"], ascending=False)
        selected = str(val_df.iloc[0]["candidate"])
        selected_val = val_df.iloc[0]
        _, test_metrics = study.simulate(
            test,
            selected,
            config,
            bankroll,
            args.min_cost,
            capture_logs=False,
            fee_mode="polymarket_taker",
            fee_rate=args.fee,
            fee_exponent=1.0,
            min_cost_policy="raise_to_min",
            min_order_mode=args.min_order_mode,
            min_order_shares=args.min_order_shares,
        )
        rows.append({
            "fold_id": fold["fold_id"],
            "validation_run": fold["validation_run"],
            "validation_runs": ",".join(fold["validation_runs"]),
            "validation_window": args.validation_window,
            "test_run": fold["test_run"],
            "selected_candidate": selected,
            "validation_score": selected_val["validation_score"],
            "validation_return": selected_val["total_return"],
            "validation_drawdown": selected_val["max_drawdown"],
            "validation_trades": selected_val["trades"],
            "starting_bankroll": bankroll,
            "ending_bankroll": test_metrics["ending_bankroll"],
            "test_return": test_metrics["total_return"],
            "test_drawdown": test_metrics["max_drawdown"],
            "test_trades": test_metrics["trades"],
            "test_win_rate": test_metrics["win_rate"],
        })
        bankroll = float(test_metrics["ending_bankroll"])
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    summary = pd.DataFrame([{
        "folds": len(result),
        "ending_bankroll": result["ending_bankroll"].iloc[-1],
        "total_test_trades": result["test_trades"].sum(),
        "positive_test_folds": int((result["test_return"] > 0).sum()),
        "zero_test_folds": int((result["test_trades"] == 0).sum()),
        "max_fold_drawdown": result["test_drawdown"].max(),
    }])
    summary_path = out.with_name(out.stem + "_summary.csv")
    summary.to_csv(summary_path, index=False)
    selections = result["selected_candidate"].value_counts().rename_axis("candidate").reset_index(name="selected_folds")
    selections.to_csv(out.with_name(out.stem + "_selection_counts.csv"), index=False)
    print(json.dumps({"complete_runs": len(runs), "fold_count": len(folds), "bankroll0": args.bankroll0, "min_cost": args.min_cost, "fee": args.fee, "min_order_mode": args.min_order_mode, "min_order_shares": args.min_order_shares, "validation_window": args.validation_window}, indent=2))
    print(summary.to_string(index=False))
    print(selections.to_string(index=False))


if __name__ == "__main__":
    main()
