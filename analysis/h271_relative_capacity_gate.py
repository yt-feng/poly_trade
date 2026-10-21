"""H271: relative executable-capacity gate on the frozen H30/H38 paper paths.

This is the second predeclared execution-contract hypothesis after H270.  It
keeps the strict one-position ledger and admits an entry only when executable
capacity divided by fee-aware minimum order cash is at least 1, 2, or 3.
Signals, source rows, costs, release clock, and no-concatenation semantics are
unchanged.  No live order is submitted.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h53_prefix_only_compound_audit import (
    CANDIDATES,
    INITIAL_CASH,
    MINIMUM_ORDER,
    DELAY_SECONDS,
    SLIPPAGES,
    add_frozen_candidates,
    chain_summary,
    paper_events,
)

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
DEPTH_FLOORS = (100.0, 150.0)
CAPACITY_RATIOS = (1.0, 2.0, 3.0)
REQUIRED_FILLS = 10
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def chronological_blocks(runs: list[str]) -> list[list[str]]:
    out, cursor = [], 0
    for size in HOLDOUT_BLOCK_SIZES:
        out.append(runs[cursor : cursor + size])
        cursor += size
    if cursor != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    return out


def replay(frame: pd.DataFrame, candidate: str, names: list[str], slippage: float, depth: float, ratio: float) -> dict[str, object]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage), min_depth=float(depth)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        min_order_shares=5.0,
        single_position_only=True,
        minimum_capacity_ratio=float(ratio),
    )
    return {
        "candidate": candidate,
        "slippage": float(slippage),
        "depth_floor": float(depth),
        "capacity_ratio": float(ratio),
        **metrics,
        **chain_summary(paper_events(logs)),
    }


def _group_passes(rows: list[dict[str, object]]) -> bool:
    expected = len(SLIPPAGES) * len(DEPTH_FLOORS) * len(CAPACITY_RATIOS)
    return bool(rows) and len(rows) == expected and all(
        int(row["first_ten_fills"]) == REQUIRED_FILLS and bool(row["first_ten_passed"])
        for row in rows
    )


def _holdout_block_passes(rows: list[dict[str, object]]) -> bool:
    """Require every contiguous holdout block and every execution cell to pass.

    The pooled ``holdout59`` prefix is a useful diagnostic, but it can hide a
    chronological gap.  A promotion-facing holdout result must therefore
    contain exactly one complete cell group for each of the five blocks and
    every row in every group must fund the same ten-row paper chain.
    """
    expected_per_block = len(SLIPPAGES) * len(DEPTH_FLOORS) * len(CAPACITY_RATIOS)
    expected_splits = {f"holdout_block_{index}" for index in range(1, len(HOLDOUT_BLOCK_SIZES) + 1)}
    if not rows or {str(row.get("split")) for row in rows} != expected_splits:
        return False
    for split in expected_splits:
        group = [row for row in rows if row.get("split") == split]
        if len(group) != expected_per_block:
            return False
        if not all(
            int(row["first_ten_fills"]) == REQUIRED_FILLS and bool(row["first_ten_passed"])
            for row in group
        ):
            return False
    return True


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits += [(f"holdout_block_{i}", block) for i, block in enumerate(chronological_blocks(holdout_runs), 1)]
    rows: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for split, names in splits:
            for ratio in CAPACITY_RATIOS:
                for slippage in SLIPPAGES:
                    for depth in DEPTH_FLOORS:
                        rows.append({"split": split, **replay(frame, candidate, names, slippage, depth, ratio)})
    selection = [row for row in rows if row["split"] == "selection20"]
    eligible = []
    for candidate in CANDIDATES:
        group = [row for row in selection if row["candidate"] == candidate]
        if _group_passes(group):
            eligible.append({
                "candidate": candidate,
                "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
            })
    choice = sorted(eligible, key=lambda row: (-row["worst_first_ten_cash_usd"], row["candidate"]))[0] if eligible else None
    chosen_holdout = [row for row in rows if row["split"] == "holdout59" and choice and row["candidate"] == choice["candidate"]]
    chosen_holdout_blocks = [
        row for row in rows
        if str(row["split"]).startswith("holdout_block_") and choice and row["candidate"] == choice["candidate"]
    ]
    pooled_holdout_passed = _group_passes(chosen_holdout)
    holdout_block_gate_passed = _holdout_block_passes(chosen_holdout_blocks)
    holdout_block_prefix_growth_passed = bool(chosen_holdout_blocks) and all(
        float(row["first_ten_ending_cash_usd"]) >= INITIAL_CASH for row in chosen_holdout_blocks
    )
    summary = {
        "format": "old-clean-h271-relative-capacity-gate-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(CANDIDATES),
        "new_hypothesis": "capacity_usd / fee_aware_required_cash >= 1, 2, or 3, with strict single-position turnover",
        "selection_rule": "Require all 12 selection cells to pass the exact ten-fill same-ledger paper prefix; rank by worst tenth-fill cash; evaluate pooled holdout and every contiguous holdout block once.",
        "selection_choice": choice,
        "selection_eligible_count": len(eligible),
        "holdout_pooled_passed": pooled_holdout_passed,
        "holdout_block_gate_passed": holdout_block_gate_passed,
        "holdout_passed": pooled_holdout_passed and holdout_block_gate_passed,
        "holdout_prefix_growth_passed": bool(chosen_holdout) and all(float(row["first_ten_ending_cash_usd"]) >= INITIAL_CASH for row in chosen_holdout),
        "holdout_block_prefix_growth_passed": holdout_block_prefix_growth_passed,
        "decision": (
            "h271_pooled_holdout_passes_but_contiguous_blocks_reject"
            if pooled_holdout_passed and not holdout_block_gate_passed
            else "h271_holdout_and_contiguous_blocks_pass_paper_only"
            if pooled_holdout_passed and holdout_block_gate_passed
            else "h271_selection_or_pooled_holdout_reject"
        ),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_shares": 5.0,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "capacity_ratios": list(CAPACITY_RATIOS),
            "same_ledger": True,
            "single_position_only": True,
            "no_concatenation": True,
            "no_refit": True,
            "paper_only": True,
        },
        "rows": len(rows),
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean raw quote/depth paper replay; no authenticated order, fill, settlement or owner cash-release receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("selection_choice", "selection_eligible_count", "holdout_pooled_passed", "holdout_block_gate_passed", "holdout_passed", "holdout_prefix_growth_passed", "holdout_block_prefix_growth_passed", "decision")}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
