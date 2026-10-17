"""Evaluate registered candidates with one global cash and execution state."""
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
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--min-order-mode", choices=("notional", "shares", "both"), default="notional")
    parser.add_argument("--min-order-shares", type=float, default=5.0)
    args = parser.parse_args()
    features, runs = prepare(Path(args.source_root), list(CANDIDATES))
    test_runs = runs[4:]
    test = features[features["run_name"].isin(test_runs)].copy()
    config = study.ExecutionConfig()
    summaries = []
    logs = []
    for candidate in CANDIDATES:
        trade_log, metrics = study.simulate(
            test,
            candidate,
            config,
            args.bankroll0,
            args.min_cost,
            capture_logs=True,
            fee_mode="polymarket_taker",
            fee_rate=0.07,
            fee_exponent=1.0,
            min_cost_policy="raise_to_min",
            min_order_mode=args.min_order_mode,
            min_order_shares=args.min_order_shares,
        )
        summaries.append({
            "strategy": candidate,
            "test_runs": len(test_runs),
            "ending_bankroll": metrics["ending_bankroll"],
            "total_return": metrics["total_return"],
            "trades": metrics["trades"],
            "max_drawdown": metrics["max_drawdown"],
            "profit_factor": metrics["profit_factor"],
            "win_rate": metrics["win_rate"],
            "zero_trade_runs": int(len(test_runs) - trade_log["run_name"].nunique()) if not trade_log.empty else len(test_runs),
        })
        if not trade_log.empty:
            trade_log["strategy"] = candidate
            logs.append(trade_log)
    result = pd.DataFrame(summaries).sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    if logs:
        pd.concat(logs, ignore_index=True).to_csv(out.with_name(out.stem + "_trade_logs.csv"), index=False)
    meta = {"complete_runs": len(runs), "test_runs": len(test_runs), "bankroll0": args.bankroll0, "min_cost": args.min_cost, "min_order_mode": args.min_order_mode, "min_order_shares": args.min_order_shares, "continuous_state": True}
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n")
    print(json.dumps(meta, sort_keys=True))
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
