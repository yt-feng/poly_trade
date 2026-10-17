"""Prefix-select a causal history-gated candidate, then freeze it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from causal_registered_gate_sweep import DEFAULT_CANDIDATES, attach_gate
from classic_min_order_chain import prepare


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--selection-runs", type=int, default=20)
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    candidates = tuple(x.strip() for x in args.candidates.split(",") if x.strip())
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), list(candidates))
    test_runs = runs[4:]
    prefix_runs = test_runs[:args.selection_runs]
    selection_rows = []
    frozen = {}
    for candidate in candidates:
        features, diagnostic = attach_gate(base, candidate, args.min_history, args.lookback)
        test = features[features["run_name"].isin(test_runs)].copy()
        prefix = test[test["run_name"].isin(prefix_runs)].copy()
        strategy = f"{candidate}_history_net_positive"
        _, metrics = study.simulate(prefix, strategy, study.ExecutionConfig(fixed_slippage=0.0025), args.bankroll0, args.min_cost, capture_logs=False, fee_mode="polymarket_taker", fee_rate=0.07, fee_exponent=1.0, min_cost_policy="raise_to_min", min_order_mode="notional", min_order_shares=5.0)
        score = float(metrics["total_return"] - 0.5 * metrics["max_drawdown"])
        selection_rows.append({"candidate":candidate,"selection_runs":args.selection_runs,"prefix_ending_bankroll":float(metrics["ending_bankroll"]),"prefix_return":float(metrics["total_return"]),"prefix_drawdown":float(metrics["max_drawdown"]),"prefix_trades":int(metrics["trades"]),"selection_score":score,"history_observations":diagnostic["history_observations"]})
        frozen[candidate] = (features, strategy)
    selection = pd.DataFrame(selection_rows).sort_values("selection_score", ascending=False).reset_index(drop=True)
    selected = str(selection.iloc[0]["candidate"])
    selected_features, strategy = frozen[selected]
    test = selected_features[selected_features["run_name"].isin(test_runs)].copy()
    run_index = {name:i for i,name in enumerate(test_runs)}
    confirm_rows, blocks = [], []
    for slip in slippages:
        logs, metrics = study.simulate(test, strategy, study.ExecutionConfig(fixed_slippage=slip), args.bankroll0, args.min_cost, capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07, fee_exponent=1.0, min_cost_policy="raise_to_min", min_order_mode="notional", min_order_shares=5.0)
        confirm_rows.append({"selected_candidate":selected,"slippage":slip,"ending_bankroll":float(metrics["ending_bankroll"]),"total_return":float(metrics["total_return"]),"trades":int(metrics["trades"]),"max_drawdown":float(metrics["max_drawdown"]),"profit_factor":float(metrics["profit_factor"]),"win_rate":float(metrics["win_rate"])})
        if not logs.empty:
            logs=logs.copy(); logs["test_index"]=logs["run_name"].map(run_index)
            for block,start,end in [("selection20",0,args.selection_runs),("holdout_next20",args.selection_runs,min(args.selection_runs+20,len(test_runs))),("holdout_next20b",min(args.selection_runs+20,len(test_runs)),min(args.selection_runs+40,len(test_runs))),("holdout_last",min(args.selection_runs+40,len(test_runs)),len(test_runs))]:
                part=logs[logs["test_index"].between(start,end-1)]
                blocks.append({"selected_candidate":selected,"slippage":slip,"block":block,"trades":int(len(part)),"ending_bankroll":float(part["bankroll_after"].iloc[-1]) if not part.empty else float("nan"),"sum_pnl":float(part["pnl_usd"].sum()) if not part.empty else 0.0,"win_rate":float((part["pnl_usd"]>0).mean()) if not part.empty else float("nan")})
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True); selection.to_csv(out.with_name(out.stem+"_selection.csv"),index=False); pd.DataFrame(confirm_rows).to_csv(out,index=False); pd.DataFrame(blocks).to_csv(out.with_name(out.stem+"_blocks.csv"),index=False)
    meta={"complete_runs":int(len(runs)),"test_runs":int(len(test_runs)),"selection_runs":args.selection_runs,"candidates":list(candidates),"selected_candidate":selected,"min_history":args.min_history,"lookback":args.lookback,"slippages":slippages,"bankroll0":args.bankroll0,"min_cost":args.min_cost,"selection_score":"prefix_return - 0.5 * prefix_drawdown","frozen_after_prefix":True,"continuous_state":True,"causal_history_only":True,"old_clean_dataset_only":True,"new_data_collected":False,"live_orders_enabled":False}
    out.with_name(out.stem+"_meta.json").write_text(json.dumps(meta,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(meta,sort_keys=True)); print(selection.to_string(index=False)); print(pd.DataFrame(confirm_rows).to_string(index=False)); print(pd.DataFrame(blocks).to_string(index=False))


if __name__ == "__main__": main()
