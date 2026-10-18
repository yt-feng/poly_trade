"""Lookback sensitivity for one frozen registered candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from causal_registered_gate_sweep import attach_gate
from classic_min_order_chain import prepare


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate", default="classic_early_drop_down")
    parser.add_argument("--lookbacks", default="10,20,30,50,100")
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    lookbacks = [int(x.strip()) for x in args.lookbacks.split(",") if x.strip()]
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), [args.candidate])
    test_runs = runs[4:]
    rows = []
    for lookback in lookbacks:
        features, diagnostic = attach_gate(base, args.candidate, args.min_history, lookback)
        test = features[features["run_name"].isin(test_runs)].copy()
        strategy = f"{args.candidate}_history_net_positive"
        for slip in slippages:
            _, metrics = study.simulate(test, strategy, study.ExecutionConfig(fixed_slippage=slip), args.bankroll0, args.min_cost, capture_logs=False, fee_mode="polymarket_taker", fee_rate=0.07, fee_exponent=1.0, min_cost_policy="raise_to_min", min_order_mode="notional", min_order_shares=5.0)
            rows.append({"candidate":args.candidate,"lookback":lookback,"slippage":slip,"ending_bankroll":float(metrics["ending_bankroll"]),"total_return":float(metrics["total_return"]),"trades":int(metrics["trades"]),"max_drawdown":float(metrics["max_drawdown"]),"profit_factor":float(metrics["profit_factor"]),"win_rate":float(metrics["win_rate"]),"history_observations":diagnostic["history_observations"]})
    result=pd.DataFrame(rows).sort_values(["slippage","ending_bankroll"],ascending=[True,False]).reset_index(drop=True)
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True); result.to_csv(out,index=False)
    meta={"complete_runs":int(len(runs)),"test_runs":int(len(test_runs)),"candidate":args.candidate,"lookbacks":lookbacks,"min_history":args.min_history,"slippages":slippages,"bankroll0":args.bankroll0,"min_cost":args.min_cost,"gate":"causal_net_positive_history","continuous_state":True,"causal_history_only":True,"old_clean_dataset_only":True,"new_data_collected":False,"live_orders_enabled":False}
    out.with_name(out.stem+"_meta.json").write_text(json.dumps(meta,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(meta,sort_keys=True)); print(result.to_string(index=False))


if __name__ == "__main__": main()
