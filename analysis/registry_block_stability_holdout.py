"""Select the registered strategy by chronological selection-block stability.

This is a separate preregistered challenger to the aggregate-selection run.
The fixed 20-run selection segment is divided into five four-run blocks.  A
candidate must have at least ten selection trades and keep every selection
block at or above the $5 floor with non-negative net return.  Only the single
winner is then evaluated on the untouched 59-run holdout and its five blocks.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from cash_release_sensitivity import simulate_cash_locked
import v1_execution_walk_forward as study
from registry_small_cash_holdout import (
    BLOCK_SIZES,
    DELAY_SECONDS,
    EXPECTED_COMPLETE_RUNS,
    INITIAL_CASH,
    MINIMUM_ORDER,
    REGISTRY_NAMES,
    SLIPPAGE,
    TAKER_FEE_RATE,
    add_signals,
    holdout_blocks,
    prepare_registry_data,
)


SELECTION_BLOCK_SIZES = (4, 4, 4, 4, 4)


def selection_blocks(runs: list[str]) -> list[list[str]]:
    if len(runs) != sum(SELECTION_BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_SELECTION20")
    blocks: list[list[str]] = []
    cursor = 0
    for size in SELECTION_BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = prepare_registry_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    selection_parts = selection_blocks(selection_runs)
    holdout_parts = holdout_blocks(holdout_runs)

    rows: list[dict[str, object]] = []
    for strategy in REGISTRY_NAMES:
        frame = add_signals(features, strategy)
        splits = [("selection20", selection_runs)]
        splits.extend((f"selection_block{index}", names) for index, names in enumerate(selection_parts, 1))
        splits.append(("holdout59", holdout_runs))
        splits.extend((f"holdout_block{index}", names) for index, names in enumerate(holdout_parts, 1))
        for split, names in splits:
            _logs, metrics = simulate_cash_locked(
                frame[frame["run_name"].isin(names)],
                strategy,
                study.ExecutionConfig(fixed_slippage=SLIPPAGE),
                INITIAL_CASH,
                MINIMUM_ORDER,
                DELAY_SECONDS,
                cooldown_events=0,
                capture_logs=False,
                fee_mode="polymarket_taker",
                fee_rate=TAKER_FEE_RATE,
            )
            rows.append({"strategy": strategy, "split": split, **metrics})

    result = pd.DataFrame(rows)
    selection = result[result["split"].eq("selection20")].set_index("strategy")
    selection_blocks_frame = result[result["split"].str.startswith("selection_block")]
    stable_names = []
    for strategy in REGISTRY_NAMES:
        agg = selection.loc[strategy]
        blocks = selection_blocks_frame[selection_blocks_frame["strategy"].eq(strategy)]
        if (
            int(agg["trades"]) >= 10
            and len(blocks) == len(SELECTION_BLOCK_SIZES)
            and (blocks["ending_cash"] >= MINIMUM_ORDER).all()
            and (blocks["net_return"] >= 0.0).all()
        ):
            stable_names.append(strategy)
    stable = selection.loc[stable_names] if stable_names else pd.DataFrame()
    selected_strategy = None
    if not stable.empty:
        stable = stable.copy()
        stable["minimum_selection_block_cash"] = stable.index.map(
            selection_blocks_frame.groupby("strategy")["ending_cash"].min()
        )
        stable = stable.sort_values(
            ["minimum_selection_block_cash", "ending_cash", "trades"],
            ascending=[False, False, False],
        )
        selected_strategy = str(stable.index[0])

    selected = result[result["strategy"].eq(selected_strategy)] if selected_strategy else pd.DataFrame()
    holdout = selected[selected["split"].eq("holdout59")].iloc[0] if not selected.empty else None
    holdout_blocks_frame = selected[selected["split"].str.startswith("holdout_block")] if not selected.empty else pd.DataFrame()
    gate_passed = bool(
        holdout is not None
        and int(holdout["trades"]) >= 10
        and len(holdout_blocks_frame) == len(BLOCK_SIZES)
        and (holdout_blocks_frame["ending_cash"] >= MINIMUM_ORDER).all()
        and (holdout_blocks_frame["net_return"] >= 0.0).all()
    )

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "candidate_metrics.csv", index=False)
    result[result["split"].str.startswith("selection")].to_csv(output / "selection_metrics.csv", index=False)
    if not selected.empty:
        selected.to_csv(output / "selected_metrics.csv", index=False)
    meta: dict[str, object] = {
        "format": "old-data-registered-strategy-block-stability-holdout-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "registry_names": list(REGISTRY_NAMES),
        "selection_runs": len(selection_runs),
        "selection_block_sizes": list(SELECTION_BLOCK_SIZES),
        "holdout_runs": len(holdout_runs),
        "holdout_block_sizes": list(BLOCK_SIZES),
        "selection_rule": "require >=10 selection trades and all five four-run selection blocks ending >=$5 with non-negative net; rank by minimum selection-block cash, then aggregate cash, then trades",
        "protocol": {
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "taker_fee_rate": TAKER_FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippage": SLIPPAGE,
            "cooldown_events": 0,
        },
        "stable_selection_candidates": stable_names,
        "selected_strategy": selected_strategy,
        "summary": {
            "stable_selection_candidates": len(stable_names),
            "holdout_trades": None if holdout is None else int(holdout["trades"]),
            "holdout_ending_cash": None if holdout is None else float(holdout["ending_cash"]),
            "minimum_holdout_block_ending_cash": None if holdout_blocks_frame.empty else float(holdout_blocks_frame["ending_cash"].min()),
            "minimum_holdout_block_net_return": None if holdout_blocks_frame.empty else float(holdout_blocks_frame["net_return"].min()),
            "gate_passed": gate_passed,
        },
        "decision": "promote_only_if_block_stable_selection_and_independent_holdout_gate_pass",
        "candidate_promoted_to_canary": False,
        "evidence_boundary": "Historical old-data paper replay only; no order acceptance, settlement, reusable-cash receipt, or Canary fill.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
