"""H196: run holdout for the strongest ten-fill paper survivor.

H159 stopped before holdout because none of its fraction-ladder cells reached
the separate $1,000 target.  That left an evidence gap for the user's nearer
gate: ten consecutive compounding fills on one evolving ledger.  H196 keeps
H159's source, signal families, costs, depth floors, release delay and fraction
ladder unchanged, selects only on the selection split, and then runs the
selected paper survivor through the complete holdout and fixed chronological
blocks.  It never submits an order or upgrades paper evidence to Canary.
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
import h159_fraction_ladder_exact_target_chain as h159


SOURCE_COMMIT = h159.SOURCE_COMMIT
REQUIRED_FILLS = h159.REQUIRED_FILLS
SLIPPAGES = h159.SLIPPAGES
DEPTH_FLOORS = h159.DEPTH_FLOORS
HOLDOUT_BLOCK_SIZES = h159.HOLDOUT_BLOCK_SIZES
CANDIDATES = h159.CANDIDATES
FRACTIONS = h159.FRACTIONS
INITIAL_CASH = h159.INITIAL_CASH


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    return h159.chronological_blocks(runs, sizes)


def select_prefix_survivor(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the strongest all-cell ten-fill survivor using selection rows only."""
    eligible: list[dict[str, Any]] = []
    expected = len(SLIPPAGES) * len(DEPTH_FLOORS)
    for strategy in sorted({str(row["strategy"]) for row in rows}):
        group = [row for row in rows if str(row["strategy"]) == strategy]
        if len(group) != expected:
            continue
        if not all(bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS for row in group):
            continue
        eligible.append({
            "candidate": group[0]["candidate"],
            "strategy": strategy,
            "fraction": float(group[0]["fraction"]),
            "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
            "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
        })
    return sorted(
        eligible,
        key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["strategy"]),
    )[0] if eligible else None


def _growth_gate(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(
        bool(row["first_ten_passed"])
        and int(row["first_ten_fills"]) == REQUIRED_FILLS
        and float(row["first_ten_ending_cash_usd"]) >= INITIAL_CASH
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
    frame, variants = h159.add_fraction_variants(frame)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    selection_rows: list[dict[str, Any]] = []
    for strategy in variants:
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                selection_rows.append({
                    "split": "selection20",
                    **h159.replay(frame, strategy, selection_runs, slippage, depth_floor),
                })
    choice = select_prefix_survivor(selection_rows)

    holdout_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    if choice:
        strategy = str(choice["strategy"])
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                holdout_rows.append({
                    "split": "holdout59",
                    **h159.replay(frame, strategy, holdout_runs, slippage, depth_floor),
                })
        for block_index, names in enumerate(holdout_blocks, start=1):
            for slippage in SLIPPAGES:
                for depth_floor in DEPTH_FLOORS:
                    block_rows.append({
                        "split": f"holdout_block_{block_index}",
                        "block_index": block_index,
                        **h159.replay(frame, strategy, names, slippage, depth_floor),
                    })

    holdout_ten_fill_passed = _growth_gate(holdout_rows)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) * len(DEPTH_FLOORS) and _growth_gate(block_rows)
    decision = "promising_paper_survivor_but_receipts_required" if holdout_ten_fill_passed and block_gate else "reject_h196_holdout_or_block_gate"
    result: dict[str, Any] = {
        "format": "old-clean-h196-prefix-survivor-holdout-v1",
        "hypothesis_id": "H196_prefix_survivor_holdout",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "candidates": list(CANDIDATES),
        "fraction_ladder": list(FRACTIONS),
        "selection_rule": "select the fraction with the highest worst-cell first-ten cash among selection-only variants that pass exactly ten same-ledger paper fills in every declared cell; no $1,000 requirement and no holdout tuning",
        "selection_choice": choice,
        "selection_rows": selection_rows,
        "holdout_rows": holdout_rows,
        "holdout_block_rows": block_rows,
        "holdout_ten_fill_passed": holdout_ten_fill_passed,
        "holdout_block_gate_passed": block_gate,
        "decision": decision,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": h159.MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": h159.MINIMUM_ORDER_SHARES,
            "fee_rate": h159.FEE_RATE,
            "cash_release_delay_seconds": h159.CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "same_ledger": True,
            "no_concatenation": True,
            "no_holdout_tuning": True,
            "paper_only": True,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean registered-signal paper replay; no authenticated order, fill, settlement or owner receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_metrics.csv", index=False)
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selection_choice": choice, "holdout_ten_fill_passed": holdout_ten_fill_passed, "holdout_block_gate_passed": block_gate, "decision": decision}, ensure_ascii=False, sort_keys=True))
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
