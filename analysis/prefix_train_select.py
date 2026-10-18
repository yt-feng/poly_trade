"""Select one registered candidate on a chronological prefix and freeze it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import CANDIDATES, prepare


def run(frame: pd.DataFrame, candidate: str, config: study.ExecutionConfig,
        bankroll: float, min_cost: float, mode: str, shares: float) -> dict:
    _, metrics = study.simulate(
        frame,
        candidate,
        config,
        bankroll,
        min_cost,
        capture_logs=False,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        fee_exponent=1.0,
        min_cost_policy="raise_to_min",
        min_order_mode=mode,
        min_order_shares=shares,
    )
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train-runs", type=int, default=20)
    parser.add_argument("--validation-runs", type=int, default=20)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--min-order-mode", choices=("notional", "shares", "both"), default="notional")
    parser.add_argument("--min-order-shares", type=float, default=5.0)
    args = parser.parse_args()
    if args.train_runs < 3 or args.validation_runs < 1:
        raise SystemExit("train-runs must be at least 3 and validation-runs must be positive")
    features, runs = prepare(Path(args.source_root), list(CANDIDATES))
    needed = args.train_runs + args.validation_runs + 1
    if len(runs) < needed:
        raise SystemExit(f"need at least {needed} complete runs, found {len(runs)}")
    train_names = runs[:args.train_runs]
    validation_names = runs[args.train_runs:args.train_runs + args.validation_runs]
    test_names = runs[args.train_runs + args.validation_runs:]
    config = study.ExecutionConfig()
    train = features[features["run_name"].isin(train_names)].copy()
    validation = features[features["run_name"].isin(validation_names)].copy()
    test = features[features["run_name"].isin(test_names)].copy()
    scores = []
    for candidate in CANDIDATES:
        metrics = run(train, candidate, config, args.bankroll0, args.min_cost,
                      args.min_order_mode, args.min_order_shares)
        score = float(metrics["total_return"]) - 0.5 * float(metrics["max_drawdown"])
        scores.append({"candidate": candidate, "selection_score": score, **metrics})
    score_df = pd.DataFrame(scores).sort_values(
        ["selection_score", "total_return", "trades"], ascending=False
    ).reset_index(drop=True)
    selected = str(score_df.iloc[0]["candidate"])
    winner_train = score_df.iloc[0].to_dict()
    winner_train.pop("candidate", None)
    validation_metrics = run(validation, selected, config, args.bankroll0, args.min_cost,
                              args.min_order_mode, args.min_order_shares)
    validation_end = float(validation_metrics["ending_bankroll"])
    test_metrics = run(test, selected, config, validation_end, args.min_cost,
                       args.min_order_mode, args.min_order_shares)
    result = {
        "complete_runs": len(runs),
        "train_runs": len(train_names),
        "validation_runs": len(validation_names),
        "test_runs": len(test_names),
        "selected_candidate": selected,
        "selection_score": float(score_df.iloc[0]["selection_score"]),
        "min_cost": args.min_cost,
        "min_order_mode": args.min_order_mode,
        "min_order_shares": args.min_order_shares,
        "train": winner_train,
        "validation": validation_metrics,
        "test": test_metrics,
        "continuous_ending_bankroll": float(test_metrics["ending_bankroll"]),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, sort_keys=True, indent=2, default=float) + "\n")
    score_df.to_csv(out.with_name(out.stem + "_candidate_scores.csv"), index=False)
    print(json.dumps(result, sort_keys=True, indent=2, default=float))


if __name__ == "__main__":
    main()
