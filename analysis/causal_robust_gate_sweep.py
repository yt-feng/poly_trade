"""Predeclared robustness gates for the frozen early-drop candidate.

The gates use only already-closed prior signals.  Candidate choice is made on
the first 20 chronological test runs; the remaining 59 runs are reported as
holdout evidence.  This is an old-clean-dataset-only diagnostic, not live
execution or canary evidence.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from causal_registered_gate_sweep import prior_net_return
from classic_min_order_chain import prepare


CANDIDATE = "classic_early_drop_down"
GATES = ("mean_positive", "lcb_half_se", "lower_quartile_positive")


def attach_robust_gate(features: pd.DataFrame, gate: str, min_history: int, lookback: int) -> tuple[pd.DataFrame, dict]:
    out = features.sort_values("first_quote_ts").copy()
    name = f"{CANDIDATE}_{gate}"
    history: list[float] = []
    for idx, row in out.iterrows():
        recent = history[-lookback:]
        if len(recent) < min_history:
            allow = False
        else:
            values = np.asarray(recent, dtype=float)
            mean = float(np.mean(values))
            if gate == "mean_positive":
                allow = mean > 0.0
            elif gate == "lcb_half_se":
                std = float(np.std(values, ddof=1))
                se = std / math.sqrt(len(values))
                allow = mean - 0.5 * se > 0.0
            elif gate == "lower_quartile_positive":
                allow = float(np.quantile(values, 0.25)) > 0.0
            else:
                raise ValueError(f"unknown gate: {gate}")
        out.at[idx, f"{name}_side"] = out.at[idx, f"{CANDIDATE}_side"] if allow else "skip"
        out.at[idx, f"{name}_minute"] = int(out.at[idx, f"{CANDIDATE}_minute"])
        out.at[idx, f"{name}_fraction"] = float(out.at[idx, f"{CANDIDATE}_fraction"])
        out.at[idx, f"{name}_component"] = name
        value = prior_net_return(row, CANDIDATE, 0.0025)
        if value is not None:
            history.append(float(value))
    return out, {"gate": gate, "history_observations": len(history), "min_history": min_history, "lookback": lookback}


def run_metrics(features: pd.DataFrame, runs: list[str], strategy: str, slip: float, bankroll0: float, min_cost: float):
    frame = features[features["run_name"].isin(runs)].copy()
    return study.simulate(
        frame,
        strategy,
        study.ExecutionConfig(fixed_slippage=slip),
        bankroll0,
        min_cost,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        fee_exponent=1.0,
        min_cost_policy="raise_to_min",
        min_order_mode="notional",
        min_order_shares=5.0,
    )


def bootstrap_run_factors(logs: pd.DataFrame, test_runs: list[str], bankroll0: float, replicates: int, seed: int) -> dict:
    end_by_run: dict[str, float] = {}
    if not logs.empty:
        for run_name, group in logs.groupby("run_name", sort=False):
            end_by_run[str(run_name)] = float(group.sort_values("first_quote_ts")["bankroll_after"].iloc[-1])
    factors: list[float] = []
    current = float(bankroll0)
    for run_name in test_runs:
        end = end_by_run.get(str(run_name), current)
        factors.append(float(end / current) if current > 0 else 1.0)
        current = end
    arr = np.asarray(factors, dtype=float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(arr), size=(replicates, len(arr)))
    finals = bankroll0 * np.exp(np.log(np.maximum(arr, 1e-300))[draws].sum(axis=1))
    return {
        "run_count": len(test_runs),
        "nonflat_runs": int(np.sum(np.abs(arr - 1.0) > 1e-12)),
        "min_run_factor": float(np.min(arr)),
        "q025": float(np.quantile(finals, 0.025)),
        "q050": float(np.quantile(finals, 0.50)),
        "prob_final_ge_100": float(np.mean(finals >= 100.0)),
        "prob_final_lt_5": float(np.mean(finals < 5.0)),
        "replicates": replicates,
        "seed": seed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--selection-runs", type=int, default=20)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), [CANDIDATE])
    test_runs = runs[4:]
    selection_runs = test_runs[: args.selection_runs]
    holdout_runs = test_runs[args.selection_runs :]
    prepared = {}
    selection_rows = []
    for gate in GATES:
        features, diagnostic = attach_robust_gate(base, gate, args.min_history, args.lookback)
        strategy = f"{CANDIDATE}_{gate}"
        _, metrics = run_metrics(features, selection_runs, strategy, 0.0025, args.bankroll0, args.min_cost)
        score = float(metrics["total_return"] - 0.5 * metrics["max_drawdown"])
        prepared[gate] = (features, strategy, diagnostic)
        selection_rows.append({
            "gate": gate,
            "selection_runs": args.selection_runs,
            "ending_bankroll": float(metrics["ending_bankroll"]),
            "total_return": float(metrics["total_return"]),
            "max_drawdown": float(metrics["max_drawdown"]),
            "trades": int(metrics["trades"]),
            "selection_score": score,
            **diagnostic,
        })
    selection = pd.DataFrame(selection_rows).sort_values("selection_score", ascending=False).reset_index(drop=True)
    selected_gate = str(selection.iloc[0]["gate"])
    rows, blocks, confidence = [], [], []
    for gate in GATES:
        features, strategy, diagnostic = prepared[gate]
        run_index = {name: i for i, name in enumerate(test_runs)}
        for slip in slippages:
            logs, metrics = run_metrics(features, test_runs, strategy, slip, args.bankroll0, args.min_cost)
            row = {
                "gate": gate,
                "slippage": slip,
                "ending_bankroll": float(metrics["ending_bankroll"]),
                "total_return": float(metrics["total_return"]),
                "trades": int(metrics["trades"]),
                "max_drawdown": float(metrics["max_drawdown"]),
                "profit_factor": float(metrics["profit_factor"]),
                "win_rate": float(metrics["win_rate"]),
                **diagnostic,
            }
            rows.append(row)
            if not logs.empty:
                logged = logs.copy()
                logged["test_index"] = logged["run_name"].map(run_index)
                for block, start, end in [("selection20", 0, args.selection_runs), ("holdout", args.selection_runs, len(test_runs))]:
                    part = logged[logged["test_index"].between(start, end - 1)]
                    blocks.append({
                        "gate": gate,
                        "slippage": slip,
                        "block": block,
                        "trades": int(len(part)),
                        "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0,
                        "win_rate": float((part["pnl_usd"] > 0).mean()) if not part.empty else float("nan"),
                    })
            confidence.append({"gate": gate, "slippage": slip, **bootstrap_run_factors(logs, test_runs, args.bankroll0, args.replicates, args.seed)})
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    selection.to_csv(out.with_name(out.stem + "_selection.csv"), index=False)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    pd.DataFrame(confidence).to_csv(out.with_name(out.stem + "_confidence.csv"), index=False)
    meta = {
        "complete_runs": len(runs),
        "test_runs": len(test_runs),
        "selection_runs": args.selection_runs,
        "holdout_runs": len(holdout_runs),
        "candidate": CANDIDATE,
        "gates": list(GATES),
        "selected_gate": selected_gate,
        "min_history": args.min_history,
        "lookback": args.lookback,
        "slippages": slippages,
        "bankroll0": args.bankroll0,
        "min_cost": args.min_cost,
        "selection_score": "selection total_return - 0.5 * selection max_drawdown",
        "bootstrap_unit": "complete chronological test run factor",
        "causal_history_only": True,
        "continuous_state": True,
        "old_clean_dataset_only": True,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "interpretation": "gated paper diagnostics; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(selection.to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(confidence).to_string(index=False))


if __name__ == "__main__":
    main()
