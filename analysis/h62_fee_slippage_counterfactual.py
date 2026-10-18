"""H62: diagnose H61 failures with fee/slippage counterfactuals.

This is a targeted cause audit, not a new selection.  It reuses the frozen
H59 runner-up and only the nine H61 failure cells; each cell is replayed with
zero fee, zero slippage, and both.  The nominal H61 rows are never re-run, so
the experiment does not accumulate duplicate baseline replays or promote a
counterfactual to a Canary.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from cash_release_sensitivity import simulate_cash_locked
from h55_tick_depth_prefix_audit import (
    CASH_RELEASE_DELAY_SECONDS,
    INITIAL_CASH,
    MINIMUM_ORDER_NOTIONAL,
    MINIMUM_ORDER_SHARES,
    chain_summary,
    paper_events,
    round_quote_prices,
)
from h56_rolling_window_stability import WINDOW_SIZE, WINDOW_STRIDE, rolling_windows
from h60_fast_focused_registry_verification import (
    add_signals,
    prepare_fast_data,
)
import v1_execution_walk_forward as study

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
STRATEGY = "v1_active_fill_mix"
FROZEN_H61_FAILED_CELLS = (
    (1, "nominal"),
    (2, "nominal"),
    (2, "stress"),
    (5, "nominal"),
    (5, "stress"),
    (6, "nominal"),
    (6, "stress"),
    (7, "stress"),
)
COUNTERFACTUALS = (
    {"label": "zero_fee", "fee_rate": 0.0, "slippage_delta": None},
    {"label": "zero_slippage", "fee_rate": 0.07, "slippage_delta": 0.0},
    {"label": "zero_fee_zero_slippage", "fee_rate": 0.0, "slippage_delta": 0.0},
)


def replay_counterfactual(
    frame: pd.DataFrame,
    names: list[str],
    config: dict[str, Any],
    counterfactual: dict[str, Any],
) -> dict[str, object]:
    slippage = float(config["slippage"] if counterfactual["slippage_delta"] is None else counterfactual["slippage_delta"])
    rounded = round_quote_prices(frame, float(config["tick_size"]))
    logs, metrics = simulate_cash_locked(
        rounded[rounded["run_name"].astype(str).isin([str(name) for name in names])],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=slippage, min_depth=float(config["depth_floor"])),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=float(counterfactual["fee_rate"]),
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {**metrics, **chain_summary(paper_events(logs))}


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, runs = prepare_fast_data(source_root)
    frame = add_signals(features, STRATEGY)
    holdout_runs = runs[4 + 20 :]
    windows = rolling_windows(holdout_runs)
    configs = {
        "nominal": {"label": "nominal", "tick_size": 0.005, "depth_floor": 100.0, "slippage": 0.005},
        "stress": {"label": "stress", "tick_size": 0.01, "depth_floor": 150.0, "slippage": 0.01},
    }

    rows: list[dict[str, object]] = []
    for window_index, config_label in FROZEN_H61_FAILED_CELLS:
        if window_index > len(windows):
            raise RuntimeError(f"H61_WINDOW_INDEX_OUT_OF_RANGE:{window_index}")
        start, names = windows[window_index - 1]
        config = configs[config_label]
        for counterfactual in COUNTERFACTUALS:
            metrics = replay_counterfactual(frame, names, config, counterfactual)
            rows.append(
                {
                    "strategy": STRATEGY,
                    "window_index": window_index,
                    "config_label": config_label,
                    "counterfactual": counterfactual["label"],
                    "window_start_offset": start,
                    "window_end_offset_exclusive": start + WINDOW_SIZE,
                    "window_start_run": names[0],
                    "window_end_run": names[-1],
                    **metrics,
                }
            )
    result = pd.DataFrame(rows)
    expected_rows = len(FROZEN_H61_FAILED_CELLS) * len(COUNTERFACTUALS)
    if len(result) != expected_rows:
        raise RuntimeError(f"RESULT_ROW_COUNT_MISMATCH:{len(result)}:{expected_rows}")
    passed = result["first_ten_passed"].astype(bool)
    fee_repaired = bool(result.loc[result["counterfactual"].eq("zero_fee"), "first_ten_passed"].any())
    slippage_repaired = bool(result.loc[result["counterfactual"].eq("zero_slippage"), "first_ten_passed"].any())
    both_repaired = bool(result.loc[result["counterfactual"].eq("zero_fee_zero_slippage"), "first_ten_passed"].any())
    if both_repaired:
        root_cause = "At least one H61 failure cell is repaired only by the optimistic zero-fee/zero-slippage counterfactual; execution cost contributes, but this does not establish executable maker fills."
    elif fee_repaired or slippage_repaired:
        root_cause = "Some H61 failure cells improve under an isolated zero-fee or zero-slippage counterfactual, but no cell passes the fully optimistic combined counterfactual; signal/regime instability remains."
    else:
        root_cause = "No H61 failure cell passes any zero-fee or zero-slippage counterfactual; the blocker is signal/regime instability rather than the modeled fee/slippage cost."
    summary = {
        "format": "old-clean-h62-fee-slippage-counterfactual-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "strategy": STRATEGY,
        "frozen_failure_cells": [{"window_index": i, "config_label": c} for i, c in FROZEN_H61_FAILED_CELLS],
        "counterfactuals": list(COUNTERFACTUALS),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate_nominal": 0.07,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "same_ledger_per_window": True,
            "required_fills": 10,
            "window_size_runs": WINDOW_SIZE,
            "window_stride_runs": WINDOW_STRIDE,
            "window_concatenation": False,
            "selection_repeated": False,
            "promotion_allowed": False,
        },
        "rows": result.to_dict(orient="records"),
        "fee_repaired_any": fee_repaired,
        "slippage_repaired_any": slippage_repaired,
        "both_repaired_any": both_repaired,
        "root_cause": root_cause,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "counterfactual_metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(result), "fee_repaired_any": fee_repaired, "slippage_repaired_any": slippage_repaired, "both_repaired_any": both_repaired}, sort_keys=True))
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
