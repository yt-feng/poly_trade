"""Predeclared loss-cooldown sensitivity on the old clean dataset."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_robust_gate_sweep import attach_robust_gate
from classic_min_order_chain import prepare


GATES = ("mean_positive", "lower_quartile_positive")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cooldowns", default="0,6,12,24,48")
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    cooldowns = [int(x.strip()) for x in args.cooldowns.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), ["classic_early_drop_down"])
    test_runs = runs[4:]
    rows, blocks = [], []
    for gate in GATES:
        features, diagnostic = attach_robust_gate(base, gate, min_history=20, lookback=20)
        strategy = f"classic_early_drop_down_{gate}"
        for cooldown in cooldowns:
            logs, metrics = simulate_cash_locked(
                features[features["run_name"].isin(test_runs)],
                strategy,
                study.ExecutionConfig(fixed_slippage=args.slippage),
                args.bankroll0,
                args.min_cost,
                cash_release_delay_seconds=0.0,
                cooldown_events=cooldown,
            )
            rows.append({"gate": gate, "slippage": args.slippage, **diagnostic, **metrics})
            if not logs.empty:
                logs = logs.copy()
                order = {name: i for i, name in enumerate(test_runs)}
                logs["test_index"] = logs["run_name"].map(order)
                for block, lo, hi in [("first20", 0, 20), ("holdout", 20, len(test_runs))]:
                    part = logs[logs["test_index"].between(lo, hi - 1)]
                    blocks.append({
                        "gate": gate,
                        "cooldown_events": cooldown,
                        "block": block,
                        "trades": int(len(part)),
                        "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0,
                        "win_rate": float((part["pnl_usd"] > 0).mean()) if not part.empty else float("nan"),
                    })
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    meta = {
        "complete_runs": len(runs), "test_runs": len(test_runs), "gates": list(GATES),
        "cooldowns_events": cooldowns, "slippage": args.slippage,
        "bankroll0": args.bankroll0, "min_cost": args.min_cost,
        "old_clean_dataset_only": True, "new_data_collected": False,
        "causal_history_only": True, "cash_release_delay_seconds": 0.0,
        "live_orders_enabled": False, "interpretation": "regime-cooldown diagnostic; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(blocks).to_string(index=False))


if __name__ == "__main__":
    main()
