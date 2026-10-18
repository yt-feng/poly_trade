"""Lookback sensitivity for the predeclared causal micro history gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare
from causal_micro_history_gate import attach_gate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--lookbacks", default="20,50,100,200")
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    lookbacks = [int(x.strip()) for x in args.lookbacks.split(",") if x.strip()]
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), ["micro_book_consensus_up"])
    test_runs = runs[4:]
    rows = []
    blocks = []
    for lookback in lookbacks:
        features, diagnostic = attach_gate(base, "net_positive", args.min_history, lookback)
        test = features[features["run_name"].isin(test_runs)].copy()
        strategy = "micro_history_net_positive"
        run_index = {name: i for i, name in enumerate(test_runs)}
        for slip in slippages:
            logs, metrics = study.simulate(
                test, strategy, study.ExecutionConfig(fixed_slippage=slip), args.bankroll0,
                args.min_cost, capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07,
                fee_exponent=1.0, min_cost_policy="raise_to_min", min_order_mode="notional",
                min_order_shares=5.0,
            )
            rows.append({"lookback": lookback, "slippage": slip, "ending_bankroll": float(metrics["ending_bankroll"]), "total_return": float(metrics["total_return"]), "trades": int(metrics["trades"]), "max_drawdown": float(metrics["max_drawdown"]), "profit_factor": float(metrics["profit_factor"]), "win_rate": float(metrics["win_rate"]), "history_observations": diagnostic["history_observations"]})
            logs = logs.copy()
            if not logs.empty:
                logs["test_index"] = logs["run_name"].map(run_index)
                for block, start, end in [("first20",0,20),("next20",20,40),("next20b",40,60),("last19",60,79)]:
                    part = logs[logs["test_index"].between(start, end - 1)]
                    blocks.append({"lookback":lookback,"slippage":slip,"block":block,"trades":int(len(part)),"ending_bankroll":float(part["bankroll_after"].iloc[-1]) if not part.empty else float("nan"),"sum_pnl":float(part["pnl_usd"].sum()) if not part.empty else 0.0,"win_rate":float((part["pnl_usd"]>0).mean()) if not part.empty else float("nan")})
    result = pd.DataFrame(rows).sort_values(["slippage", "ending_bankroll"], ascending=[True, False]).reset_index(drop=True)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    meta = {"complete_runs": int(len(runs)), "test_runs": int(len(test_runs)), "lookbacks": lookbacks, "min_history": args.min_history, "slippages": slippages, "bankroll0": args.bankroll0, "min_cost": args.min_cost, "gate": "net_positive", "continuous_state": True, "causal_history_only": True, "old_clean_dataset_only": True, "new_data_collected": False, "live_orders_enabled": False}
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
