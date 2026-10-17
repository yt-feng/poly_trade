"""Cross-slippage audit for the prefix-selected frozen cooldown candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_robust_gate_sweep import attach_robust_gate
from classic_min_order_chain import prepare


def bootstrap(logs: pd.DataFrame, runs: list[str], bankroll0: float, reps: int, seed: int) -> dict:
    pnl = logs.groupby("run_name")["pnl_usd"].sum().to_dict() if not logs.empty else {}
    current = float(bankroll0)
    factors = []
    for name in runs:
        end = max(current + float(pnl.get(name, 0.0)), 1e-9)
        factors.append(end / current if current > 0 else 1.0)
        current = end
    arr = np.asarray(factors, dtype=float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(arr), size=(reps, len(arr)))
    finals = bankroll0 * np.exp(np.log(np.maximum(arr, 1e-300))[draws].sum(axis=1))
    return {"run_count": len(runs), "nonflat_runs": int(np.sum(np.abs(arr - 1.0) > 1e-12)), "min_run_factor": float(np.min(arr)), "q025": float(np.quantile(finals, .025)), "q050": float(np.quantile(finals, .50)), "prob_final_ge_100": float(np.mean(finals >= 100.0)), "prob_final_lt_5": float(np.mean(finals < 5.0)), "replicates": reps, "seed": seed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), ["classic_early_drop_down"])
    test_runs = runs[4:]
    features, diagnostic = attach_robust_gate(base, "mean_positive", min_history=20, lookback=20)
    strategy = "classic_early_drop_down_mean_positive"
    rows, blocks, confidence = [], [], []
    order = {name: i for i, name in enumerate(test_runs)}
    for slip in slippages:
        logs, metrics = simulate_cash_locked(features[features["run_name"].isin(test_runs)], strategy, study.ExecutionConfig(fixed_slippage=slip), args.bankroll0, args.min_cost, 0.0, 12)
        rows.append({"candidate":"classic_early_drop_down_history_net_positive_lookback20_cooldown12", "slippage":slip, **metrics, **diagnostic})
        if not logs.empty:
            logged = logs.copy(); logged["test_index"] = logged["run_name"].map(order)
            for block, lo, hi in [("selection20",0,20),("holdout59",20,len(test_runs))]:
                part=logged[logged["test_index"].between(lo,hi-1)]
                blocks.append({"slippage":slip,"block":block,"trades":int(len(part)),"sum_pnl":float(part["pnl_usd"].sum()) if not part.empty else 0.0,"win_rate":float((part["pnl_usd"]>0).mean()) if not part.empty else float("nan")})
        confidence.append({"slippage":slip,**bootstrap(logs,test_runs,args.bankroll0,args.replicates,args.seed)})
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(rows).to_csv(out,index=False); pd.DataFrame(blocks).to_csv(out.with_name(out.stem+"_blocks.csv"),index=False); pd.DataFrame(confidence).to_csv(out.with_name(out.stem+"_confidence.csv"),index=False)
    meta={"candidate":"classic_early_drop_down_history_net_positive_lookback20_cooldown12","complete_runs":len(runs),"test_runs":len(test_runs),"slippages":slippages,"bankroll0":args.bankroll0,"min_cost":args.min_cost,"cooldown_events":12,"old_clean_dataset_only":True,"new_data_collected":False,"causal_history_only":True,"cash_release_delay_seconds":0.0,"live_orders_enabled":False,"interpretation":"frozen cross-slippage paper diagnostic; not canary evidence"}
    out.with_name(out.stem+"_meta.json").write_text(json.dumps(meta,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(meta,sort_keys=True)); print(pd.DataFrame(rows).to_string(index=False)); print(pd.DataFrame(confidence).to_string(index=False))


if __name__ == "__main__": main()
