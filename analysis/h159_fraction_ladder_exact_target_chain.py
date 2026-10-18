"""H159: pre-registered position-fraction ladder for the strongest H158 signals.

H158 showed that ``classic_breakout_up`` and ``micro_book_consensus_up`` can
survive ten chronological paper fills, but their fixed registry fractions do
not approach the requested $10-to-$1,000 endpoint.  H159 changes only the
position fraction through a frozen ladder and keeps the signal, source split,
fee, depth, slippage and cash-release contract unchanged.

The ladder is selected only on selection20 when every declared cost/depth cell
reaches the target.  Holdout never tunes the fraction.  Every result remains a
paper upper bound on one evolving cash ledger; no blocks are concatenated and
no order is submitted.
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
INITIAL_CASH = h158.INITIAL_CASH
TARGET_CASH = h158.TARGET_CASH
REQUIRED_FILLS = h158.REQUIRED_FILLS
MINIMUM_ORDER_NOTIONAL = h158.MINIMUM_ORDER_NOTIONAL
MINIMUM_ORDER_SHARES = h158.MINIMUM_ORDER_SHARES
FEE_RATE = h158.FEE_RATE
CASH_RELEASE_DELAY_SECONDS = h158.CASH_RELEASE_DELAY_SECONDS
SLIPPAGES = h158.SLIPPAGES
DEPTH_FLOORS = h158.DEPTH_FLOORS
HOLDOUT_BLOCK_SIZES = h158.HOLDOUT_BLOCK_SIZES
CANDIDATES = h158.CANDIDATES
# Frozen before running; the ladder spans the registry fraction and progressively
# more aggressive reinvestment without selecting a value from holdout labels.
FRACTIONS = (0.05, 0.10, 0.20, 0.40, 0.80)


def fraction_label(value: float) -> str:
    return f"{value:.2f}".replace(".", "p")


def variant_name(candidate: str, fraction: float) -> str:
    return f"{candidate}__fraction_{fraction_label(fraction)}"


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    return h158.chronological_blocks(runs, sizes)


def add_fraction_variants(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    out = frame.copy()
    variants: list[str] = []
    for candidate in CANDIDATES:
        for fraction in FRACTIONS:
            name = variant_name(candidate, fraction)
            variants.append(name)
            out[f"{name}_side"] = out[f"{candidate}_side"]
            out[f"{name}_minute"] = out[f"{candidate}_minute"]
            active = out[f"{candidate}_side"].astype(str).ne("skip")
            fraction_values = pd.Series(0.0, index=out.index, dtype=float)
            fraction_values.loc[active] = float(fraction)
            out[f"{name}_fraction"] = fraction_values
            out[f"{name}_component"] = f"{candidate}_fraction_{fraction_label(fraction)}"
    return out, variants


def replay(frame: pd.DataFrame, strategy: str, names: list[str], slippage: float, depth_floor: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        strategy,
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
    base_candidate, fraction_text = strategy.rsplit("__fraction_", 1)
    fraction = float(fraction_text.replace("p", "."))
    return {
        "candidate": base_candidate,
        "strategy": strategy,
        "fraction": fraction,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        **metrics,
        **h158.chain_summary(logs),
    }


def configs_pass(rows: list[dict[str, Any]], *, target: bool) -> bool:
    return h158.configs_pass(rows, target=target)


def select(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible: list[dict[str, Any]] = []
    for strategy in sorted({str(row["strategy"]) for row in rows}):
        group = [row for row in rows if row["strategy"] == strategy]
        if not configs_pass(group, target=True):
            continue
        eligible.append(
            {
                "candidate": group[0]["candidate"],
                "strategy": strategy,
                "fraction": float(group[0]["fraction"]),
                "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
                "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
            }
        )
    return sorted(eligible, key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["strategy"]))[0] if eligible else None


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, runs = registry.prepare(source_root, list(CANDIDATES))
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame, variants = add_fraction_variants(frame)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    selection_rows: list[dict[str, Any]] = []
    for strategy in variants:
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                selection_rows.append({"split": "selection20", **replay(frame, strategy, selection_runs, slippage, depth_floor)})

    diagnostics: list[dict[str, Any]] = []
    for strategy in variants:
        group = [row for row in selection_rows if row["strategy"] == strategy]
        diagnostics.append(
            {
                "candidate": group[0]["candidate"],
                "strategy": strategy,
                "fraction": group[0]["fraction"],
                "best_first_ten_cash_usd": max(float(row["first_ten_ending_cash_usd"]) for row in group),
                "max_fills": max(int(row["first_ten_fills"]) for row in group),
                "target_configs": sum(bool(row["first_ten_target_growth_passed"]) for row in group),
            }
        )
    best_selection = max(diagnostics, key=lambda row: (row["best_first_ten_cash_usd"], row["max_fills"], row["strategy"]))
    choice = select(selection_rows)

    holdout_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    if choice:
        strategy = str(choice["strategy"])
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                holdout_rows.append({"split": "holdout59", **replay(frame, strategy, holdout_runs, slippage, depth_floor)})
        for block_index, names in enumerate(holdout_blocks, start=1):
            for slippage in SLIPPAGES:
                for depth_floor in DEPTH_FLOORS:
                    block_rows.append({"split": f"holdout_block_{block_index}", "block_index": block_index, **replay(frame, strategy, names, slippage, depth_floor)})

    holdout_ten = configs_pass(holdout_rows, target=False)
    holdout_target = configs_pass(holdout_rows, target=True)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"])
        and int(row["first_ten_fills"]) == REQUIRED_FILLS
        and bool(row["first_ten_target_growth_passed"])
        and float(row["first_ten_minimum_cash_usd"]) >= MINIMUM_ORDER_NOTIONAL
        for row in block_rows
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h159-fraction-ladder-exact-target-chain-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "candidates": list(CANDIDATES),
        "fraction_ladder": list(FRACTIONS),
        "candidate_protocol": "H158 classic_breakout_up and micro_book_consensus_up signals; only position fraction varies; no refit",
        "selection_rule": "select only a candidate/fraction whose every declared fee/depth/slippage selection cell has exactly ten chronological fills and ending cash >= $1000; holdout never selects",
        "selection_choice": choice,
        "best_selection_diagnostics": best_selection,
        "selection_diagnostics": diagnostics,
        "selection_rows": selection_rows,
        "holdout_rows": holdout_rows,
        "holdout_block_rows": block_rows,
        "holdout_ten_fill_passed": holdout_ten,
        "holdout_target_growth_passed": holdout_target,
        "holdout_block_gate_passed": block_gate,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "target_cash_usd": TARGET_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "same_ledger": True,
            "no_concatenation": True,
            "variable_per_fill_multipliers": True,
            "no_holdout_tuning": True,
            "paper_only": True,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean registered-signal paper replay with pre-registered fraction ladder; no authenticated order, fill, settlement or owner receipt",
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
