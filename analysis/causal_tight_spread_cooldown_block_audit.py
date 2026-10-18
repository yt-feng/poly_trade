"""Audit a predeclared tighter cooldown for the causal tight-spread candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from causal_entry_gate_prefix_selection import PROFILES, add_causal_candidate, prepare_once
from causal_entry_spread_prefix_selection import SPREAD_CANDIDATE, add_spread_candidate
from causal_registered_gate_sweep import attach_gate
from cash_release_sensitivity import simulate_cash_locked

BLOCKS = 5
BASE_COOLDOWN = 12
CHALLENGER_COOLDOWN = 48


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--delay", type=float, default=600.0)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    source_root = Path(args.source_root)
    features, runs = prepare_once(source_root)
    features = add_spread_candidate(add_causal_candidate(features))
    test_runs = runs[4:]
    base_size, remainder = divmod(len(test_runs), BLOCKS)
    sizes = [base_size + (i < remainder) for i in range(BLOCKS)]
    run_blocks, cursor = [], 0
    for i, size in enumerate(sizes, 1):
        names = test_runs[cursor:cursor + size]
        run_blocks.append((i, names))
        cursor += size

    gated, diagnostic = attach_gate(features, SPREAD_CANDIDATE, min_history=20, lookback=20)
    strategy = f"{SPREAD_CANDIDATE}_history_net_positive"
    rows = []
    envelope = PROFILES["baseline"]
    for cooldown in (BASE_COOLDOWN, CHALLENGER_COOLDOWN):
        for block_index, names in run_blocks:
            _, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(names)], strategy,
                study.ExecutionConfig(fixed_slippage=args.slippage, **envelope),
                args.bankroll0, args.min_cost, args.delay, cooldown, capture_logs=True,
            )
            rows.append({
                "candidate": "causal_1m_spread_tight04",
                "cooldown_events": cooldown, "block_index": block_index,
                "block_runs": len(names), "first_run": names[0], "last_run": names[-1],
                "slippage": args.slippage, "cash_release_delay_seconds": args.delay,
                **metrics, **{f"diagnostic_{k}": v for k, v in diagnostic.items()},
            })

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    meta = {
        "format": "old-data-causal-tight-spread-cooldown-block-audit-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "test_runs": len(test_runs),
        "blocks": BLOCKS, "block_sizes": sizes, "cooldowns_tested": [BASE_COOLDOWN, CHALLENGER_COOLDOWN],
        "slippage": args.slippage, "cash_release_delay_seconds": args.delay,
        "settlement_seconds": 300.0, "bankroll0": args.bankroll0, "min_cost": args.min_cost,
        "execution_profile": envelope, "old_clean_dataset_only": True, "new_data_collected": False,
        "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "interpretation": "predeclared cooldown block audit; no refit and not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
