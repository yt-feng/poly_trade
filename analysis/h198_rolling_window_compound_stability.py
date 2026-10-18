"""H198: rolling-window stability audit for one-ledger ten-fill chains.

H197 showed that a registered strategy can pass an aggregate holdout prefix
while its non-overlapping chronological blocks lose opportunity continuity.
H198 measures that failure mode without changing the signal, fitting a model,
collecting data, or selecting on holdout: the frozen H30 candidate and the
already-registered H197 challenger are replayed in predeclared overlapping
12-run windows under the frozen fee, depth, share-floor and cash-release
contract. The narrowed pair keeps the audit within a bounded cloud budget;
it is a diagnostic comparison, not a new strategy search.

Each window starts a fresh diagnostic $10 ledger. Windows are never
concatenated and their counts are not Canary fills. This is a stability
diagnostic only; it cannot promote a strategy or authorize an order.
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

import classic_min_order_chain as registry
import h197_registry_prefix_survivor_holdout as h197

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
WINDOW_RUNS = 12
WINDOW_STRIDE = 4
ROBUST_WINDOW_FRACTION = 0.80
AUDIT_CANDIDATES = ("h30_move_uncertainty", "classic_breakout_up")


def rolling_windows(runs: list[str], window_runs: int = WINDOW_RUNS, stride: int = WINDOW_STRIDE) -> list[dict[str, Any]]:
    """Return overlapping chronological windows without resetting run order."""
    if window_runs <= 0:
        raise ValueError("WINDOW_RUNS_MUST_BE_POSITIVE")
    if stride <= 0:
        raise ValueError("WINDOW_STRIDE_MUST_BE_POSITIVE")
    if len(runs) < window_runs:
        raise ValueError("INSUFFICIENT_RUNS_FOR_WINDOW")
    return [
        {
            "window_index": index + 1,
            "start_run": str(runs[index]),
            "end_run": str(runs[index + window_runs - 1]),
            "runs": [str(name) for name in runs[index : index + window_runs]],
        }
        for index in range(0, len(runs) - window_runs + 1, stride)
    ]


def _cell_summary(rows: list[dict[str, Any]], candidate: str, slippage: float, depth_floor: float) -> dict[str, Any]:
    selected = [
        row for row in rows
        if row["candidate"] == candidate
        and float(row["slippage"]) == float(slippage)
        and float(row["depth_floor"]) == float(depth_floor)
    ]
    growth = [row for row in selected if bool(row["first_ten_growth_positive"])]
    return {
        "candidate": candidate,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        "window_count": len(selected),
        "growth_positive_windows": len(growth),
        "growth_window_fraction": len(growth) / len(selected) if selected else 0.0,
        "all_windows_growth_positive": bool(selected) and len(growth) == len(selected),
        "robust_window_gate": bool(selected) and len(growth) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "minimum_first_ten_cash_usd": min((float(row["first_ten_ending_cash_usd"]) for row in selected), default=INITIAL_CASH),
        "median_first_ten_cash_usd": float(pd.Series([float(row["first_ten_ending_cash_usd"]) for row in selected]).median()) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    unknown = sorted(set(AUDIT_CANDIDATES) - set(registry.CANDIDATES))
    if unknown:
        raise RuntimeError(f"AUDIT_CANDIDATE_NOT_REGISTERED:{unknown}")
    frame, runs = registry.prepare(source_root, list(AUDIT_CANDIDATES))
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    test_runs = runs[4:]
    windows = rolling_windows(test_runs)

    rows: list[dict[str, Any]] = []
    for window in windows:
        for candidate in AUDIT_CANDIDATES:
            for slippage in h197.SLIPPAGES:
                for depth_floor in h197.DEPTH_FLOORS:
                    rows.append({
                        "window_index": int(window["window_index"]),
                        "start_run": window["start_run"],
                        "end_run": window["end_run"],
                        "split": "rolling_holdout_window",
                        **h197.replay(frame, candidate, window["runs"], slippage, depth_floor),
                    })

    cell_summaries = [
        _cell_summary(rows, candidate, slippage, depth_floor)
        for candidate in AUDIT_CANDIDATES
        for slippage in h197.SLIPPAGES
        for depth_floor in h197.DEPTH_FLOORS
    ]
    candidate_summaries: list[dict[str, Any]] = []
    for candidate in AUDIT_CANDIDATES:
        cells = [row for row in cell_summaries if row["candidate"] == candidate]
        candidate_summaries.append({
            "candidate": candidate,
            "cells": len(cells),
            "robust_cells": sum(bool(row["robust_window_gate"]) for row in cells),
            "all_cells_robust": bool(cells) and all(bool(row["robust_window_gate"]) for row in cells),
            "minimum_growth_window_fraction": min((float(row["growth_window_fraction"]) for row in cells), default=0.0),
            "minimum_first_ten_cash_usd": min((float(row["minimum_first_ten_cash_usd"]) for row in cells), default=INITIAL_CASH),
        })

    result: dict[str, Any] = {
        "format": "old-clean-h198-rolling-window-compound-stability-v1",
        "hypothesis_id": "H198_rolling_window_compound_stability",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "candidate_count": len(AUDIT_CANDIDATES),
        "candidates": list(AUDIT_CANDIDATES),
        "window_count": len(windows),
        "window_runs": WINDOW_RUNS,
        "window_stride": WINDOW_STRIDE,
        "selection_rule": "none; the frozen H30 candidate and the already-registered H197 challenger are audited in all predeclared overlapping chronological windows; no holdout selection or tuning",
        "robust_window_gate": f"at least {ROBUST_WINDOW_FRACTION:.0%} of rolling windows are ten-fill growth-positive in each fee/depth cell; diagnostic only and does not promote a strategy",
        "cell_summaries": cell_summaries,
        "candidate_summaries": candidate_summaries,
        "window_rows": rows,
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
        "evidence_boundary": "pinned old-clean rolling-window paper diagnostic; window counts are not Canary fills and no authenticated owner, settlement or cash-release receipt exists",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "rolling_window_metrics.csv", index=False)
    pd.DataFrame(cell_summaries).to_csv(output / "cell_summaries.csv", index=False)
    pd.DataFrame(candidate_summaries).to_csv(output / "candidate_summaries.csv", index=False)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"window_count": len(windows), "candidate_summaries": candidate_summaries}, ensure_ascii=False, sort_keys=True))
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
