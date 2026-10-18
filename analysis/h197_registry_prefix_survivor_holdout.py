"""H197: audit every registered clean-data strategy under one cash ledger.

H196 closed the H30/H38 fraction family.  H197 asks the narrower remaining
+question: does any *already registered* strategy, without refitting or a new
signal family, pass ten chronological compounding fills on the selection
prefix and then survive the frozen holdout blocks?  The candidate is selected
only on selection20; all fee, depth, slippage, share-floor and cash-release
rules are fixed before replay.

This is a paper replay on the pinned clean snapshot.  It never creates an
order or upgrades paper rows to Canary evidence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import classic_min_order_chain as registry
import h158_registry_exact_variable_multiplier_target_chain as h158
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
MINIMUM_ORDER_NOTIONAL = 5.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.07
CASH_RELEASE_DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
CANDIDATES = tuple(registry.CANDIDATES)


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in sizes:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    if len({item for block in blocks for item in block}) != len(runs):
        raise ValueError("BLOCKS_MUST_NOT_OVERLAP")
    return blocks


def replay(frame: pd.DataFrame, candidate: str, names: list[str], slippage: float, depth_floor: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage), min_depth=float(depth_floor)),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    chain = h158.chain_summary(logs)
    # The first-ten paper chain must grow from the same starting ledger.  This
    # is deliberately stricter than merely counting ten trades.
    chain["first_ten_growth_positive"] = bool(
        chain["first_ten_passed"]
        and int(chain["first_ten_fills"]) == REQUIRED_FILLS
        and float(chain["first_ten_ending_cash_usd"]) >= INITIAL_CASH
    )
    return {
        "candidate": candidate,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        **metrics,
        **chain,
    }


def _complete_growth_group(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_growth_positive"])
        for row in rows
    )


def select_prefix_survivor(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        group = [row for row in rows if str(row["candidate"]) == candidate]
        if not _complete_growth_group(group):
            continue
        eligible.append(
            {
                "candidate": candidate,
                "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
                "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
            }
        )
    return sorted(
        eligible,
        key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["candidate"]),
    )[0] if eligible else None


def _pass_all(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_growth_positive"])
        for row in rows
    )


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, runs = registry.prepare(source_root, list(CANDIDATES))
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    selection_rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                selection_rows.append({
                    "split": "selection20",
                    **replay(frame, candidate, selection_runs, slippage, depth_floor),
                })
    choice = select_prefix_survivor(selection_rows)

    diagnostics = []
    for candidate in CANDIDATES:
        group = [row for row in selection_rows if row["candidate"] == candidate]
        diagnostics.append({
            "candidate": candidate,
            "selection_cells": len(group),
            "growth_cells": sum(bool(row["first_ten_growth_positive"]) for row in group),
            "max_first_ten_fills": max((int(row["first_ten_fills"]) for row in group), default=0),
            "best_first_ten_cash_usd": max((float(row["first_ten_ending_cash_usd"]) for row in group), default=INITIAL_CASH),
        })

    holdout_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    if choice:
        candidate = str(choice["candidate"])
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                holdout_rows.append({
                    "split": "holdout59",
                    **replay(frame, candidate, holdout_runs, slippage, depth_floor),
                })
        for block_index, names in enumerate(holdout_blocks, start=1):
            for slippage in SLIPPAGES:
                for depth_floor in DEPTH_FLOORS:
                    block_rows.append({
                        "split": f"holdout_block_{block_index}",
                        "block_index": block_index,
                        **replay(frame, candidate, names, slippage, depth_floor),
                    })

    holdout_gate = _pass_all(holdout_rows)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_growth_positive"])
        for row in block_rows
    )
    decision = "promising_registered_paper_survivor_receipts_required" if holdout_gate and block_gate else "reject_h197_registered_survivor"
    result: dict[str, Any] = {
        "format": "old-clean-h197-registry-prefix-survivor-holdout-v1",
        "hypothesis_id": "H197_registry_prefix_survivor_holdout",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "candidate_count": len(CANDIDATES),
        "candidates": list(CANDIDATES),
        "selection_rule": "select only an already registered candidate whose every selection20 fee/depth cell completes ten same-ledger fills with ending cash at least initial cash; rank by worst-cell first-ten cash; never tune on holdout",
        "selection_choice": choice,
        "selection_diagnostics": diagnostics,
        "selection_rows": selection_rows,
        "holdout_rows": holdout_rows,
        "holdout_block_rows": block_rows,
        "holdout_ten_fill_growth_gate_passed": holdout_gate,
        "holdout_block_gate_passed": block_gate,
        "decision": decision,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "same_ledger": True,
            "no_concatenation": True,
            "no_holdout_tuning": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean registered-strategy paper replay; no authenticated order, fill, settlement or owner receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_metrics.csv", index=False)
    pd.DataFrame(diagnostics).to_csv(output / "selection_diagnostics.csv", index=False)
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selection_choice": choice, "holdout_gate": holdout_gate, "block_gate": block_gate, "decision": decision}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
