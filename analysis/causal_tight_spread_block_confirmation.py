"""Frozen chronological block confirmation for the tight minute-one spread candidate."""
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

BLOCKS = 5


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
    if len(test_runs) % BLOCKS != 0:
        # The fixed split is deterministic and the remainder is assigned to the
        # earliest blocks, never tuned after observing returns.
        base_size, remainder = divmod(len(test_runs), BLOCKS)
        sizes = [base_size + (i < remainder) for i in range(BLOCKS)]
    else:
        sizes = [len(test_runs) // BLOCKS] * BLOCKS
    run_blocks = []
    cursor = 0
    for i, size in enumerate(sizes, 1):
        names = test_runs[cursor:cursor + size]
        run_blocks.append((i, names))
        cursor += size

    prepared = {}
    for label, candidate, rule in [
        ("causal_1m_spread", CAUSAL_CANDIDATE, "btc_move_1m <= -20 and spread_down_1m <= 0.06"),
        ("causal_1m_spread_tight04", SPREAD_CANDIDATE, "base rule and spread_down_1m <= 0.04"),
    ]:
        gated, diagnostic = attach_gate(features, candidate, min_history=20, lookback=20)
        prepared[label] = (gated, f"{candidate}_history_net_positive", {"signal_rule": rule, **diagnostic})

    rows = []
    envelope = PROFILES["baseline"]
    for label, (frame, strategy, diagnostic) in prepared.items():
        for block_index, names in run_blocks:
            logs, metrics = simulate_cash_locked(
                frame[frame["run_name"].isin(names)],
                strategy,
                study.ExecutionConfig(fixed_slippage=args.slippage, **envelope),
                args.bankroll0,
                args.min_cost,
                args.delay,
                12,
                capture_logs=True,
            )
            rows.append({
                "candidate": label, "block_index": block_index,
                "block_runs": len(names), "first_run": names[0], "last_run": names[-1],
                "slippage": args.slippage, "cash_release_delay_seconds": args.delay,
                **metrics, **{f"diagnostic_{k}": v for k, v in diagnostic.items()},
            })

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    meta = {
        "format": "old-data-causal-tight-spread-block-confirmation-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "test_runs": len(test_runs),
        "blocks": BLOCKS, "block_sizes": sizes, "slippage": args.slippage,
        "cash_release_delay_seconds": args.delay, "settlement_seconds": 300.0,
        "bankroll0": args.bankroll0, "min_cost": args.min_cost, "cooldown_events": 12,
        "execution_profile": envelope, "old_clean_dataset_only": True,
        "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
        "interpretation": "frozen chronological block consistency audit; no refit and not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
