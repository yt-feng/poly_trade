"""Stress the two causal minute-one spread candidates under cash-release delays."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from causal_entry_gate_prefix_selection import CAUSAL_CANDIDATE, PROFILES, add_causal_candidate, prepare_once
from causal_entry_spread_prefix_selection import SPREAD_CANDIDATE, add_spread_candidate
from causal_registered_gate_sweep import attach_gate
from cash_release_sensitivity import simulate_cash_locked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--delays", default="0,5,30,300,600")
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    delays = [float(x.strip()) for x in args.delays.split(",") if x.strip()]
    source_root = Path(args.source_root)
    features, runs = prepare_once(source_root)
    features = add_spread_candidate(add_causal_candidate(features))
    test_runs = runs[4:]
    order = {name: i for i, name in enumerate(test_runs)}
    prepared = {}
    for label, candidate, rule in [
        ("causal_1m_spread", CAUSAL_CANDIDATE, "btc_move_1m <= -20 and spread_down_1m <= 0.06"),
        ("causal_1m_spread_tight04", SPREAD_CANDIDATE, "base rule and spread_down_1m <= 0.04"),
    ]:
        gated, diagnostic = attach_gate(features, candidate, min_history=20, lookback=20)
        prepared[label] = (gated, f"{candidate}_history_net_positive", {"signal_rule": rule, **diagnostic})

    rows = []
    blocks = []
    envelope = PROFILES["baseline"]
    for label, (frame, strategy, diagnostic) in prepared.items():
        test = frame[frame["run_name"].isin(test_runs)]
        for delay in delays:
            logs, metrics = simulate_cash_locked(
                test,
                strategy,
                study.ExecutionConfig(fixed_slippage=args.slippage, **envelope),
                args.bankroll0,
                args.min_cost,
                delay,
                12,
                capture_logs=True,
            )
            rows.append({"candidate": label, "delay_seconds": delay, "slippage": args.slippage, **metrics, **{f"diagnostic_{k}": v for k, v in diagnostic.items()}})
            if not logs.empty:
                logs = logs.copy()
                logs["test_index"] = logs["run_name"].map(order)
            for block, lo, hi in [("selection20", 0, 20), ("holdout59", 20, len(test_runs))]:
                part = logs[logs["test_index"].between(lo, hi - 1)] if not logs.empty else logs
                blocks.append({"candidate": label, "delay_seconds": delay, "slippage": args.slippage, "block": block, "trades": int(len(part)), "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0, "win_rate": float((part["pnl_usd"] > 0).mean()) if not part.empty else float("nan")})

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    meta = {
        "format": "old-data-causal-spread-cash-release-audit-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "test_runs": len(test_runs),
        "selection_runs": 20, "holdout_runs": 59, "candidates": list(prepared),
        "delays_seconds": delays, "slippage": args.slippage, "bankroll0": args.bankroll0,
        "min_cost": args.min_cost, "cooldown_events": 12, "settlement_seconds": 300.0,
        "execution_profile": envelope, "old_clean_dataset_only": True, "new_data_collected": False,
        "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "interpretation": "cash-release transport stress of predeclared causal minute-one spread candidates; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(blocks).to_string(index=False))


if __name__ == "__main__":
    main()
