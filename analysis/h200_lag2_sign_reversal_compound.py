"""H200: paper-derived lag-sign reversal on the pinned old-clean snapshot.

The August 2026 short-horizon crypto-reversal paper reports that a simple
previous-candle sign reversal is most of its 15-minute out-of-sample signal,
while the gross edge is below benchmark round-trip cost.  H200 transfers only
that low-parameter direction hypothesis to the existing BTC five-minute
features: reverse the sign of the causal first-two-minute BTC move, enter at
minute two, and keep the frozen four-percent fraction.  The horizon and
venue-transfer limits are recorded explicitly; no paper parameter is copied.

Each overlapping window starts a fresh $10 diagnostic ledger.  The ten fills
must be chronological on that one evolving ledger; windows are never
concatenated and are not Canary fills.  This is paper-only evidence.
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
import h198_rolling_window_compound_stability as h198

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
WINDOW_RUNS = h198.WINDOW_RUNS
WINDOW_STRIDE = h198.WINDOW_STRIDE
ROBUST_WINDOW_FRACTION = h198.ROBUST_WINDOW_FRACTION
REVERSAL_CANDIDATE = "h200_lag2_sign_reversal"
ENTRY_MINUTE = 2
FRACTION = 0.04


def add_sign_reversal_candidate(features: pd.DataFrame) -> pd.DataFrame:
    """Add a parameter-free sign reversal with no look-ahead."""
    out = features.copy()
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[move.gt(0)] = "buy_down"
    side.loc[move.lt(0)] = "buy_up"
    out[f"{REVERSAL_CANDIDATE}_side"] = side
    out[f"{REVERSAL_CANDIDATE}_minute"] = ENTRY_MINUTE
    out[f"{REVERSAL_CANDIDATE}_fraction"] = FRACTION
    out[f"{REVERSAL_CANDIDATE}_component"] = "paper_derived_lag_sign_reversal"
    return out


def _cell_summary(rows: list[dict[str, Any]], slippage: float, depth_floor: float) -> dict[str, Any]:
    selected = [
        row for row in rows
        if float(row["slippage"]) == float(slippage)
        and float(row["depth_floor"]) == float(depth_floor)
    ]
    growth = [row for row in selected if bool(row["first_ten_growth_positive"])]
    return {
        "candidate": REVERSAL_CANDIDATE,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        "window_count": len(selected),
        "growth_positive_windows": len(growth),
        "growth_window_fraction": len(growth) / len(selected) if selected else 0.0,
        "all_windows_growth_positive": bool(selected) and len(growth) == len(selected),
        "robust_window_gate": bool(selected) and len(growth) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "minimum_first_ten_cash_usd": min(
            (float(row["first_ten_ending_cash_usd"]) for row in selected), default=INITIAL_CASH
        ),
        "median_first_ten_cash_usd": float(
            pd.Series([float(row["first_ten_ending_cash_usd"]) for row in selected]).median()
        ) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, runs = registry.prepare(source_root, ["classic_breakout_up"])
    frame = add_sign_reversal_candidate(frame)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    test_runs = runs[4:]
    windows = h198.rolling_windows(test_runs)

    rows: list[dict[str, Any]] = []
    for window in windows:
        for slippage in h197.SLIPPAGES:
            for depth_floor in h197.DEPTH_FLOORS:
                rows.append({
                    "window_index": int(window["window_index"]),
                    "start_run": window["start_run"],
                    "end_run": window["end_run"],
                    "split": "rolling_holdout_window",
                    **h197.replay(
                        frame, REVERSAL_CANDIDATE, window["runs"], slippage, depth_floor
                    ),
                })

    cell_summaries = [
        _cell_summary(rows, slippage, depth_floor)
        for slippage in h197.SLIPPAGES
        for depth_floor in h197.DEPTH_FLOORS
    ]
    all_cells_robust = bool(cell_summaries) and all(
        bool(row["robust_window_gate"]) for row in cell_summaries
    )
    result: dict[str, Any] = {
        "format": "old-clean-h200-lag2-sign-reversal-compound-v1",
        "hypothesis_id": "H200_LAG2_SIGN_REVERSAL_COMPOUND",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "candidate_count": 1,
        "candidates": [REVERSAL_CANDIDATE],
        "window_count": len(windows),
        "window_runs": WINDOW_RUNS,
        "window_stride": WINDOW_STRIDE,
        "selection_rule": "none; one sign-only reversal is evaluated in every predeclared overlapping chronological window; no holdout selection or tuning",
        "signal_binding": {
            "direction": "buy_down when btc_move_2m > 0; buy_up when btc_move_2m < 0; skip when zero or missing",
            "entry_minute": ENTRY_MINUTE,
            "fraction": FRACTION,
            "causal_boundary": "btc_move_2m is observed before minute-two entry; no future label or settlement field is used",
            "paper_transfer_limit": "source paper is 15-minute Binance multi-asset evidence; this is a BTC five-minute venue-transfer diagnostic, not a copied parameter claim",
        },
        "robust_window_gate": f"at least {ROBUST_WINDOW_FRACTION:.0%} of rolling windows are ten-fill growth-positive in each fee/depth cell; diagnostic only and does not promote a strategy",
        "cell_summaries": cell_summaries,
        "candidate_summary": {
            "candidate": REVERSAL_CANDIDATE,
            "cells": len(cell_summaries),
            "robust_cells": sum(bool(row["robust_window_gate"]) for row in cell_summaries),
            "all_cells_robust": all_cells_robust,
            "minimum_growth_window_fraction": min(
                (float(row["growth_window_fraction"]) for row in cell_summaries), default=0.0
            ),
            "minimum_first_ten_cash_usd": min(
                (float(row["minimum_first_ten_cash_usd"]) for row in cell_summaries), default=INITIAL_CASH
            ),
        },
        "window_rows": rows,
        "decision": (
            "paper_reversal_stability_promising_receipts_required"
            if all_cells_robust else "reject_h200_lag2_sign_reversal_rolling_window_stability"
        ),
        "references": {
            "arxiv": "https://arxiv.org/abs/2608.21888",
            "author_replication": "https://github.com/nadav2/short-horizon-reversion",
        },
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
        "evidence_boundary": "pinned old-clean rolling-window paper diagnostic; each window is one evolving ledger, but diagnostic windows are not Canary fills and no authenticated owner, settlement or cash-release receipt exists",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "rolling_window_metrics.csv", index=False)
    pd.DataFrame(cell_summaries).to_csv(output / "cell_summaries.csv", index=False)
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"window_count": len(windows), "decision": result["decision"], "candidate_summary": result["candidate_summary"]}, ensure_ascii=False, sort_keys=True))
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
