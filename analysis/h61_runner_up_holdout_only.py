"""H61: evaluate the preselected H59 runner-up on untouched rolling holdouts.

H59 already established the registry-selection evidence.  H61 spends one
research run only on ``v1_active_fill_mix`` and the nine rolling holdout
windows, preserving the exact H55/H56 same-ledger execution contract.  It is
an efficiency experiment: no registry re-selection, refit, new data or live
orders are permitted.
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

from h56_rolling_window_stability import CONFIGS, WINDOW_SIZE, WINDOW_STRIDE, rolling_windows
from h60_fast_focused_registry_verification import (
    CANDIDATES,
    add_signals,
    prepare_fast_data,
    replay,
)

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
STRATEGY = "v1_active_fill_mix"
H59_SELECTION_EVIDENCE = {
    "strategy": STRATEGY,
    "selection_blocks": 2,
    "min_selection_cash": 11.69001,
    "selection_cash_sum": 52.302929999999996,
    "source": "H59 same-ledger registry audit; stable under nominal and stress configs",
}


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    if STRATEGY not in CANDIDATES:
        raise RuntimeError("H61_CANDIDATE_NOT_IN_FROZEN_H60_SET")

    features, runs = prepare_fast_data(source_root)
    frame = add_signals(features, STRATEGY)
    test_runs = runs[4:]
    holdout_runs = test_runs[20:]
    windows = rolling_windows(holdout_runs)

    rows: list[dict[str, object]] = []
    for window_index, (start, names) in enumerate(windows, start=1):
        for config in CONFIGS:
            metrics = replay(frame, STRATEGY, names, config)
            rows.append(
                {
                    "strategy": STRATEGY,
                    "window_index": window_index,
                    "window_start_offset": start,
                    "window_end_offset_exclusive": start + WINDOW_SIZE,
                    "window_start_run": names[0],
                    "window_end_run": names[-1],
                    "split": f"holdout_window_{window_index}",
                    **config,
                    **metrics,
                }
            )
    result = pd.DataFrame(rows)
    expected_rows = len(windows) * len(CONFIGS)
    if len(result) != expected_rows:
        raise RuntimeError(f"RESULT_ROW_COUNT_MISMATCH:{len(result)}:{expected_rows}")
    holdout_gate = bool(
        len(result) == expected_rows
        and result["first_ten_passed"].all()
        and (result["first_ten_fills"] == 10).all()
    )
    summary = {
        "format": "old-clean-h61-runner-up-holdout-only-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "strategy": STRATEGY,
        "selection_evidence": H59_SELECTION_EVIDENCE,
        "selection_repeated": False,
        "protocol": {
            "initial_cash_usd": 10.0,
            "minimum_order_shares": 5,
            "fee_rate": 0.07,
            "cash_release_delay_seconds": 600,
            "configs": list(CONFIGS),
            "same_ledger_per_window": True,
            "required_fills": 10,
            "window_size_runs": WINDOW_SIZE,
            "window_stride_runs": WINDOW_STRIDE,
            "window_overlap": True,
            "window_concatenation": False,
            "refit": False,
        },
        "window_count": len(windows),
        "window_definitions": [
            {
                "window_index": index,
                "start_offset": start,
                "end_offset_exclusive": start + WINDOW_SIZE,
                "start_run": names[0],
                "end_run": names[-1],
            }
            for index, (start, names) in enumerate(windows, start=1)
        ],
        "holdout_rows": result.to_dict(orient="records"),
        "holdout_gate": holdout_gate,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "root_cause_if_failed": (
            "The H59 runner-up does not produce ten positive-growth chronological fills "
            "in every untouched rolling holdout/config; the current registry family is "
            "therefore rejected for the frozen contract."
            if not holdout_gate
            else None
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "holdout_metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"strategy": STRATEGY, "holdout_gate": holdout_gate, "window_count": len(windows)}, sort_keys=True))
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
