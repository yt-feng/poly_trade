"""Continuous old-data bankroll threshold sweep.

This does not select a winner from the sweep.  It keeps three already
registered signal families frozen and varies only starting cash, replaying the
same chronological test table in one global execution state.  The result
separates a minimum-order capital-capacity failure from a signal failure.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare


CANDIDATES = (
    "v1_conservative_mix",
    "v1_adaptive_mix",
    "micro_book_consensus_up",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bankrolls", default="10,15,20,25,50")
    parser.add_argument("--slippages", default="0.0025,0.0050")
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--min-order-mode", choices=("notional", "shares", "both"), default="notional")
    parser.add_argument("--min-order-shares", type=float, default=5.0)
    args = parser.parse_args()
    bankrolls = [float(x.strip()) for x in args.bankrolls.split(",") if x.strip()]
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    if not bankrolls or not slippages:
        raise SystemExit("bankrolls and slippages must be non-empty")
    features, runs = prepare(Path(args.source_root), list(CANDIDATES))
    test_runs = runs[4:]
    test = features[features["run_name"].isin(test_runs)].copy()
    rows = []
    for slip in slippages:
        config = study.ExecutionConfig(fixed_slippage=slip)
        for candidate in CANDIDATES:
            for bankroll0 in bankrolls:
                _, metrics = study.simulate(
                    test,
                    candidate,
                    config,
                    bankroll0,
                    args.min_cost,
                    capture_logs=False,
                    fee_mode="polymarket_taker",
                    fee_rate=0.07,
                    fee_exponent=1.0,
                    min_cost_policy="raise_to_min",
                    min_order_mode=args.min_order_mode,
                    min_order_shares=args.min_order_shares,
                )
                rows.append({
                    "candidate": candidate,
                    "starting_bankroll": bankroll0,
                    "slippage": slip,
                    "ending_bankroll": float(metrics["ending_bankroll"]),
                    "total_return": float(metrics["total_return"]),
                    "trades": int(metrics["trades"]),
                    "max_drawdown": float(metrics["max_drawdown"]),
                    "profit_factor": float(metrics["profit_factor"]),
                    "win_rate": float(metrics["win_rate"]),
                })
    result = pd.DataFrame(rows).sort_values(["slippage", "candidate", "starting_bankroll"]).reset_index(drop=True)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    meta = {
        "complete_runs": int(len(runs)),
        "test_runs": int(len(test_runs)),
        "candidates": list(CANDIDATES),
        "bankrolls": bankrolls,
        "slippages": slippages,
        "min_cost": args.min_cost,
        "min_order_mode": args.min_order_mode,
        "min_order_shares": args.min_order_shares,
        "continuous_state": True,
        "old_clean_dataset_only": True,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
