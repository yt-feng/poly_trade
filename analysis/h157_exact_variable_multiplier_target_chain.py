"""H157: exact variable-multiplier $10-to-$1,000 chain audit.

H155/H156 used a sufficient entry-time gate that required every fill to be
individually capable of the 1.584893x geometric multiplier.  That can reject a
valid chain whose gains are uneven.  H157 removes that sufficient gate and
tests the actual requested endpoint directly: one chronological ledger must
complete ten fills and finish at or above $1,000 under every declared cost and
depth configuration in selection, then once again in untouched holdout blocks.

The candidate set is frozen to the existing H30/H38 signals and the registered
causal H42/H43 protocols.  H42/H43 probabilities still use only completed
earlier runs.  This is paper-only and never concatenates blocks or submits an
order.
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

import h42_causal_calibrated_edge as h42
import h43_betabinomial_residual_calibration as h43
import h54_execution_contract_prefix_sensitivity as h54
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
TARGET_CASH = 1000.0
REQUIRED_FILLS = 10
MINIMUM_ORDER_NOTIONAL = 5.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.07
CASH_RELEASE_DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
FIXED_EDGE_BUFFER = 0.03
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
CANDIDATES = (
    "h30_move_uncertainty",
    "h38_move_uncertainty_midpath",
    h42.STRATEGY,
    h43.STRATEGY,
)


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


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ]


def chain_summary(logs: pd.DataFrame) -> dict[str, Any]:
    events = paper_events(logs)
    prefix = evaluate_compound_chain(
        events[:REQUIRED_FILLS],
        initial_cash=str(INITIAL_CASH),
        minimum_order="0.0000001",
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
    )
    endpoint = float(prefix["ending_cash"])
    return {
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash_usd": endpoint,
        "first_ten_minimum_cash_usd": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_target_growth_passed": bool(prefix["paper_replay_passed"] and endpoint >= TARGET_CASH),
        "first_ten_failure_reason": prefix["failure_reason"],
        "per_fill_geometric_multiplier": (endpoint / INITIAL_CASH) ** (1.0 / REQUIRED_FILLS) if endpoint > 0 else 0.0,
    }


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
    return {"candidate": candidate, "slippage": float(slippage), "depth_floor": float(depth_floor), **metrics, **chain_summary(logs)}


def _configs_pass(rows: list[dict[str, Any]], *, target: bool) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"])
        and int(row["first_ten_fills"]) == REQUIRED_FILLS
        and (not target or bool(row["first_ten_target_growth_passed"]))
        for row in rows
    )


def select(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        group = [row for row in rows if row["candidate"] == candidate]
        if not _configs_pass(group, target=True):
            continue
        eligible.append({
            "candidate": candidate,
            "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
            "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
        })
    return sorted(eligible, key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["candidate"]))[0] if eligible else None


def _prepare_candidates(frame: pd.DataFrame, runs: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    frozen = h54.add_frozen_candidates(frame)
    out[CANDIDATES[0]] = frozen
    out[CANDIDATES[1]] = frozen
    h42_prob, _history = h42.expanding_probabilities(frame, runs)
    out[CANDIDATES[2]] = h42.add_edge_signal(frame, h42_prob, FIXED_EDGE_BUFFER)
    h43_prob = h43.expanding_probabilities(frame, runs)
    out[CANDIDATES[3]] = h43.add_edge_signal(frame, h43_prob, FIXED_EDGE_BUFFER)
    return out


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    candidates = _prepare_candidates(frame, runs)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    selection_rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                selection_rows.append({"split": "selection20", **replay(candidates[candidate], candidate, selection_runs, slippage, depth_floor)})
    best_selection = None
    diagnostics = []
    for candidate in CANDIDATES:
        group = [row for row in selection_rows if row["candidate"] == candidate]
        if group:
            diagnostics.append({"candidate": candidate, "best_first_ten_cash_usd": max(float(row["first_ten_ending_cash_usd"]) for row in group), "max_fills": max(int(row["first_ten_fills"]) for row in group), "target_configs": sum(bool(row["first_ten_target_growth_passed"]) for row in group)})
    if diagnostics:
        best_selection = max(diagnostics, key=lambda row: (row["best_first_ten_cash_usd"], row["max_fills"], row["candidate"]))
    choice = select(selection_rows)
    holdout_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    if choice:
        candidate = str(choice["candidate"])
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                holdout_rows.append({"split": "holdout59", **replay(candidates[candidate], candidate, holdout_runs, slippage, depth_floor)})
        for block_index, names in enumerate(holdout_blocks, start=1):
            for slippage in SLIPPAGES:
                for depth_floor in DEPTH_FLOORS:
                    block_rows.append({"split": f"holdout_block_{block_index}", "block_index": block_index, **replay(candidates[candidate], candidate, names, slippage, depth_floor)})
    holdout_ten = _configs_pass(holdout_rows, target=False)
    holdout_target = _configs_pass(holdout_rows, target=True)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS and bool(row["first_ten_target_growth_passed"]) and float(row["first_ten_minimum_cash_usd"]) >= MINIMUM_ORDER_NOTIONAL
        for row in block_rows
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h157-exact-variable-multiplier-target-chain-v1",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "complete_runs": len(runs), "feature_rows": len(frame), "candidates": list(CANDIDATES),
        "candidate_protocol": "H30/H38 frozen signals; H42 expanding LogisticRegression; H43 expanding Beta-Binomial; H42/H43 fixed edge branch 0.03",
        "selection_rule": "select only if every declared fee/depth/slippage selection cell has exactly ten chronological fills and ending cash >= $1000; variable per-fill multipliers are allowed; holdout never selects",
        "selection_choice": choice, "best_selection_diagnostics": best_selection, "selection_diagnostics": diagnostics,
        "selection_rows": selection_rows, "holdout_rows": holdout_rows, "holdout_block_rows": block_rows,
        "holdout_ten_fill_passed": holdout_ten, "holdout_target_growth_passed": holdout_target, "holdout_block_gate_passed": block_gate,
        "protocol": {"initial_cash_usd": INITIAL_CASH, "target_cash_usd": TARGET_CASH, "required_fills": REQUIRED_FILLS, "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL, "minimum_order_shares": MINIMUM_ORDER_SHARES, "fee_rate": FEE_RATE, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS, "slippages": list(SLIPPAGES), "depth_floors": list(DEPTH_FLOORS), "same_ledger": True, "no_concatenation": True, "variable_per_fill_multipliers": True, "no_holdout_tuning": True, "paper_only": True},
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0, "new_data_collected": False, "model_refit": False, "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean candidate-family paper replay with exact variable-multiplier ten-fill endpoint; no authenticated order, fill, settlement or owner receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_metrics.csv", index=False)
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selection_choice": choice, "best_selection_diagnostics": best_selection, "holdout_ten_fill_passed": holdout_ten, "holdout_target_growth_passed": holdout_target, "holdout_block_gate_passed": block_gate}, ensure_ascii=False, sort_keys=True))
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
