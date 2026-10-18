"""Prefix-select fixed cooldown challengers, then audit chronological holdout."""
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


CANDIDATES = (
    ("mean_positive", 6),
    ("mean_positive", 12),
    ("lower_quartile_positive", 0),
)


def bootstrap_run_pnl(logs: pd.DataFrame, test_runs: list[str], bankroll0: float, replicates: int, seed: int) -> dict:
    pnl_by_run = logs.groupby("run_name")["pnl_usd"].sum().to_dict() if not logs.empty else {}
    current = float(bankroll0)
    factors = []
    for run in test_runs:
        pnl = float(pnl_by_run.get(run, 0.0))
        end = max(current + pnl, 1e-9)
        factors.append(end / current if current > 0 else 1.0)
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
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--selection-runs", type=int, default=20)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    base, runs = prepare(Path(args.source_root), ["classic_early_drop_down"])
    test_runs = runs[4:]
    prefix_runs = test_runs[: args.selection_runs]
    prepared = {}
    selection_rows = []
    for gate, cooldown in CANDIDATES:
        features, diagnostic = attach_robust_gate(base, gate, min_history=20, lookback=20)
        strategy = f"classic_early_drop_down_{gate}"
        prefix = features[features["run_name"].isin(prefix_runs)]
        prefix_logs, prefix_metrics = simulate_cash_locked(
            prefix, strategy, study.ExecutionConfig(fixed_slippage=args.slippage), args.bankroll0, args.min_cost, 0.0, cooldown
        )
        score = float(prefix_metrics["net_return"] - 0.5 * prefix_metrics["max_cash_drawdown"])
        key = f"{gate}_cooldown{cooldown}"
        prepared[key] = (features, strategy, gate, cooldown, diagnostic)
        selection_rows.append({
            "candidate": key,
            "gate": gate,
            "cooldown_events": cooldown,
            "selection_runs": args.selection_runs,
            "ending_cash": float(prefix_metrics["ending_cash"]),
            "net_return": float(prefix_metrics["net_return"]),
            "max_cash_drawdown": float(prefix_metrics["max_cash_drawdown"]),
            "trades": int(prefix_metrics["trades"]),
            "selection_score": score,
            **diagnostic,
        })
    selection = pd.DataFrame(selection_rows).sort_values("selection_score", ascending=False).reset_index(drop=True)
    selected = str(selection.iloc[0]["candidate"])
    rows, blocks, confidence = [], [], []
    for key, (features, strategy, gate, cooldown, diagnostic) in prepared.items():
        frame = features[features["run_name"].isin(test_runs)]
        logs, metrics = simulate_cash_locked(
            frame, strategy, study.ExecutionConfig(fixed_slippage=args.slippage), args.bankroll0, args.min_cost, 0.0, cooldown
        )
        rows.append({"candidate": key, "gate": gate, "cooldown_events": cooldown, "selected_by_prefix": key == selected, **metrics, **diagnostic})
        if not logs.empty:
            logged = logs.copy()
            order = {name: i for i, name in enumerate(test_runs)}
            logged["test_index"] = logged["run_name"].map(order)
            for block, lo, hi in [("selection20", 0, args.selection_runs), ("holdout59", args.selection_runs, len(test_runs))]:
                part = logged[logged["test_index"].between(lo, hi - 1)]
                blocks.append({
                    "candidate": key,
                    "block": block,
                    "trades": int(len(part)),
                    "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0,
                    "win_rate": float((part["pnl_usd"] > 0).mean()) if not part.empty else float("nan"),
                })
        if key == selected:
            confidence.append({"candidate": key, "gate": gate, "cooldown_events": cooldown, **bootstrap_run_pnl(logs, test_runs, args.bankroll0, args.replicates, args.seed)})
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(out.with_name(out.stem + "_selection.csv"), index=False)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    pd.DataFrame(confidence).to_csv(out.with_name(out.stem + "_confidence.csv"), index=False)
    meta = {
        "complete_runs": len(runs), "test_runs": len(test_runs), "selection_runs": args.selection_runs,
        "holdout_runs": len(test_runs) - args.selection_runs, "candidates": [f"{g}_cooldown{c}" for g, c in CANDIDATES],
        "selected_candidate": selected, "slippage": args.slippage, "bankroll0": args.bankroll0,
        "min_cost": args.min_cost, "bootstrap_unit": "complete chronological test run pnl factor",
        "old_clean_dataset_only": True, "new_data_collected": False, "causal_history_only": True,
        "cash_release_delay_seconds": 0.0, "live_orders_enabled": False,
        "interpretation": "prefix-selected paper diagnostic; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(selection.to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(confidence).to_string(index=False))


if __name__ == "__main__":
    main()
