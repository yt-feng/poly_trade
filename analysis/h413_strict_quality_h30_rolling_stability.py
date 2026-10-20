"""H413: strict-quality rolling stability audit for the frozen H30 signal.

The earlier rolling-window diagnostic used the complete old-clean snapshot but
did not apply the quality-first feature filter used by H406. H413 repairs that
evidence boundary without changing H30, fitting a model, collecting data, or
selecting on holdout. Every predeclared overlapping window starts a fresh
diagnostic $10 ledger; windows are never concatenated and none of their rows
are Canary fills.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h197_registry_prefix_survivor_holdout as h197
import h198_rolling_window_compound_stability as h198
import h54_execution_contract_prefix_sensitivity as h54
import v1_execution_walk_forward as study

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
AUDIT_CANDIDATE = "h30_move_uncertainty"
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
WINDOW_RUNS = h198.WINDOW_RUNS
WINDOW_STRIDE = h198.WINDOW_STRIDE
ROBUST_WINDOW_FRACTION = h198.ROBUST_WINDOW_FRACTION


def cell_summary(rows: list[dict[str, Any]], slippage: float, depth_floor: float) -> dict[str, Any]:
    selected = [
        row for row in rows
        if float(row["slippage"]) == float(slippage)
        and float(row["depth_floor"]) == float(depth_floor)
    ]
    growth = [row for row in selected if bool(row["first_ten_growth_positive"])]
    return {
        "candidate": AUDIT_CANDIDATE,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        "window_count": len(selected),
        "growth_positive_windows": len(growth),
        "growth_window_fraction": len(growth) / len(selected) if selected else 0.0,
        "all_windows_growth_positive": bool(selected) and len(growth) == len(selected),
        "robust_window_gate": bool(selected)
        and len(growth) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "minimum_first_ten_cash_usd": min(
            (float(row["first_ten_ending_cash_usd"]) for row in selected),
            default=INITIAL_CASH,
        ),
        "median_first_ten_cash_usd": float(
            pd.Series([float(row["first_ten_ending_cash_usd"]) for row in selected]).median()
        ) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min(
            (int(row["first_ten_fills"]) for row in selected), default=0
        ),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h54.add_frozen_candidates(features)
    test_runs = runs[4:]
    windows = h198.rolling_windows(test_runs, window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)

    rows: list[dict[str, Any]] = []
    for window in windows:
        for slippage in h197.SLIPPAGES:
            for depth_floor in h197.DEPTH_FLOORS:
                rows.append({
                    "window_index": int(window["window_index"]),
                    "start_run": window["start_run"],
                    "end_run": window["end_run"],
                    "split": "strict_quality_rolling_holdout_window",
                    **h197.replay(
                        frame,
                        AUDIT_CANDIDATE,
                        window["runs"],
                        slippage,
                        depth_floor,
                    ),
                })

    cells = [
        cell_summary(rows, slippage, depth_floor)
        for slippage in h197.SLIPPAGES
        for depth_floor in h197.DEPTH_FLOORS
    ]
    all_cells_robust = bool(cells) and all(bool(row["robust_window_gate"]) for row in cells)
    result: dict[str, Any] = {
        "format": "old-clean-h413-strict-quality-h30-rolling-stability-v1",
        "hypothesis_id": "H413_STRICT_QUALITY_H30_ROLLING_STABILITY",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "coverage_rows": len(coverage),
        "candidate": AUDIT_CANDIDATE,
        "window_count": len(windows),
        "window_runs": WINDOW_RUNS,
        "window_stride": WINDOW_STRIDE,
        "selection_rule": "none; one frozen H30 candidate is audited in every predeclared overlapping chronological window",
        "robust_window_gate": f"at least {ROBUST_WINDOW_FRACTION:.0%} of windows are ten-fill growth-positive in every fee/depth cell",
        "cell_summaries": cells,
        "candidate_summary": {
            "candidate": AUDIT_CANDIDATE,
            "cells": len(cells),
            "robust_cells": sum(bool(row["robust_window_gate"]) for row in cells),
            "all_cells_robust": all_cells_robust,
            "minimum_growth_window_fraction": min(
                (float(row["growth_window_fraction"]) for row in cells), default=0.0
            ),
            "minimum_first_ten_cash_usd": min(
                (float(row["minimum_first_ten_cash_usd"]) for row in cells),
                default=INITIAL_CASH,
            ),
        },
        "window_rows": rows,
        "decision": (
            "paper_stability_promising_receipts_required"
            if all_cells_robust
            else "reject_h413_strict_quality_rolling_stability"
        ),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": h197.MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": h197.MINIMUM_ORDER_SHARES,
            "fee_rate": h197.FEE_RATE,
            "cash_release_delay_seconds": h197.CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(h197.SLIPPAGES),
            "depth_floors": list(h197.DEPTH_FLOORS),
            "same_ledger_within_window": True,
            "fresh_diagnostic_ledger_per_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean rolling-window paper diagnostic; windows are not Canary fills and no authenticated owner, settlement or cash-release receipt exists",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "rolling_window_metrics.csv", index=False)
    pd.DataFrame(cells).to_csv(output / "cell_summaries.csv", index=False)
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"window_count": len(windows), "candidate_summary": result["candidate_summary"], "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
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
