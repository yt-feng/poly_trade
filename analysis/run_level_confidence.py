"""Run-block confidence audit for the frozen causal gated candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from causal_registered_gate_sweep import attach_gate
from classic_min_order_chain import prepare


def bootstrap_factors(factors: np.ndarray, bankroll0: float, replicates: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(factors), size=(replicates, len(factors)))
    logs = np.log(np.maximum(factors, 1e-300))
    finals = bankroll0 * np.exp(logs[draws].sum(axis=1))
    return {
        "replicates": replicates,
        "seed": seed,
        "q025": float(np.quantile(finals, 0.025)),
        "q050": float(np.quantile(finals, 0.50)),
        "q975": float(np.quantile(finals, 0.975)),
        "prob_final_ge_initial": float(np.mean(finals >= bankroll0)),
        "prob_final_ge_100": float(np.mean(finals >= 100.0)),
        "prob_final_lt_5": float(np.mean(finals < 5.0)),
        "mean_final": float(np.mean(finals)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate", default="classic_early_drop_down")
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), [args.candidate])
    features, _ = attach_gate(base, args.candidate, args.min_history, args.lookback)
    test_runs = runs[4:]
    test = features[features["run_name"].isin(test_runs)].copy()
    strategy = f"{args.candidate}_history_net_positive"
    rows = []
    for slip in slippages:
        logs, metrics = study.simulate(test, strategy, study.ExecutionConfig(fixed_slippage=slip), args.bankroll0, args.min_cost, capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07, fee_exponent=1.0, min_cost_policy="raise_to_min", min_order_mode="notional", min_order_shares=5.0)
        end_by_run = {}
        if not logs.empty:
            for run_name, group in logs.groupby("run_name", sort=False):
                end_by_run[str(run_name)] = float(group.sort_values("first_quote_ts")["bankroll_after"].iloc[-1])
        factors = []
        current = float(args.bankroll0)
        for run_name in test_runs:
            end = end_by_run.get(str(run_name), current)
            factors.append(float(end / current) if current > 0 else 1.0)
            current = end
        factors_arr = np.asarray(factors, dtype=float)
        boot = bootstrap_factors(factors_arr, args.bankroll0, args.replicates, args.seed)
        rows.append({"candidate":args.candidate,"lookback":args.lookback,"slippage":slip,"observed_ending_bankroll":float(metrics["ending_bankroll"]),"observed_trades":int(metrics["trades"]),"observed_max_drawdown":float(metrics["max_drawdown"]),"observed_profit_factor":float(metrics["profit_factor"]),"nonflat_runs":int(np.sum(np.abs(factors_arr-1.0)>1e-12),),"min_run_factor":float(np.min(factors_arr)),"max_run_factor":float(np.max(factors_arr)),**boot})
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True); pd.DataFrame(rows).to_csv(out,index=False)
    meta={"complete_runs":int(len(runs)),"test_runs":int(len(test_runs)),"candidate":args.candidate,"lookback":args.lookback,"min_history":args.min_history,"slippages":slippages,"bankroll0":args.bankroll0,"min_cost":args.min_cost,"replicates":args.replicates,"seed":args.seed,"bootstrap_unit":"complete chronological test run factor","continuous_state":True,"causal_history_only":True,"old_clean_dataset_only":True,"new_data_collected":False,"live_orders_enabled":False,"interpretation":"uncertainty diagnostic, not an independent trial or canary evidence"}
    out.with_name(out.stem+"_meta.json").write_text(json.dumps(meta,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(meta,sort_keys=True)); print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__": main()
