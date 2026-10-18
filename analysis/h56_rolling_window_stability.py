"""H56: rolling-window stability of the frozen ten-fill compound contract.

H55 passed the aggregate selection20/holdout59 prefix under tick/depth/slippage
stress but showed unstable short blocks. H56 predeclares overlapping 20-run
holdout windows and tests each candidate from a fresh *window-local* ledger.
Window results are never concatenated and no signal is refit.
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
import h55_tick_depth_prefix_audit as h55

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
WINDOW_SIZE = 20
WINDOW_STRIDE = 5
CONFIGS = (
    {"label": "nominal", "tick_size": 0.005, "depth_floor": 100.0, "slippage": 0.005},
    {"label": "stress", "tick_size": 0.01, "depth_floor": 150.0, "slippage": 0.01},
)


def rolling_windows(names: list[str]) -> list[tuple[int, list[str]]]:
    if len(names) < WINDOW_SIZE:
        raise ValueError(f"HOLDOUT_TOO_SHORT:{len(names)}")
    starts = list(range(0, len(names) - WINDOW_SIZE + 1, WINDOW_STRIDE))
    tail = len(names) - WINDOW_SIZE
    if starts[-1] != tail:
        starts.append(tail)
    return [(start, names[start : start + WINDOW_SIZE]) for start in starts]


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)

    rows: list[dict[str, object]] = []
    for candidate in h55.CANDIDATES:
        for window_index, (start, names) in enumerate(windows, start=1):
            for config in CONFIGS:
                metrics, _logs = h55.replay(
                    frame,
                    candidate,
                    names,
                    config["tick_size"],
                    config["depth_floor"],
                    config["slippage"],
                )
                rows.append(
                    {
                        "candidate": candidate,
                        "window_index": window_index,
                        "window_start_offset": start,
                        "window_end_offset_exclusive": start + WINDOW_SIZE,
                        "window_start_run": names[0],
                        "window_end_run": names[-1],
                        **config,
                        **metrics,
                    }
                )
    result = pd.DataFrame(rows)
    expected_rows = len(h55.CANDIDATES) * len(windows) * len(CONFIGS)
    gates: dict[str, dict[str, object]] = {}
    for candidate in h55.CANDIDATES:
        candidate_rows = result[result["candidate"].eq(candidate)]
        complete = len(candidate_rows) == len(windows) * len(CONFIGS)
        all_windows_pass = bool(
            complete
            and candidate_rows["first_ten_passed"].all()
            and (candidate_rows["first_ten_fills"] == 10).all()
        )
        gates[candidate] = {
            "rolling_window_exact_ten_gate": all_windows_pass,
            "window_count": len(windows),
            "config_count": len(CONFIGS),
            "failed_windows": candidate_rows[~candidate_rows["first_ten_passed"]].to_dict(orient="records"),
            "rows": candidate_rows.to_dict(orient="records"),
        }

    summary = {
        "format": "old-clean-h56-rolling-window-stability-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(h55.CANDIDATES),
        "frozen_signal": {
            "score": "btc_move_2m * (1 - 2*abs(mid_up_prob_2m - 0.5))",
            "low_threshold": h55.LOW_THRESHOLD,
            "high_threshold": h55.HIGH_THRESHOLD,
            "entry_minute": h55.ENTRY_MINUTE,
            "fraction": h55.FRACTION,
        },
        "protocol": {
            "initial_cash_usd": h55.INITIAL_CASH,
            "minimum_order_notional_usd": h55.MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": h55.MINIMUM_ORDER_SHARES,
            "fee_rate": h55.FEE_RATE,
            "cash_release_delay_seconds": h55.CASH_RELEASE_DELAY_SECONDS,
            "configs": list(CONFIGS),
            "same_ledger_per_window": True,
            "required_fills": 10,
            "window_size_runs": WINDOW_SIZE,
            "window_stride_runs": WINDOW_STRIDE,
            "window_overlap": True,
            "window_concatenation": False,
            "refit": False,
        },
        "documentation_sources": h55.DOCS,
        "primary_target": "every predeclared rolling holdout window completes ten chronological fills with positive same-ledger growth",
        "acceptance": "both frozen candidates pass every rolling window under nominal and stress configs",
        "window_count": len(windows),
        "window_definitions": [
            {"window_index": index, "start_offset": start, "end_offset_exclusive": start + WINDOW_SIZE, "start_run": names[0], "end_run": names[-1]}
            for index, (start, names) in enumerate(windows, start=1)
        ],
        "gates": gates,
        "all_candidates_rolling_gate": bool(all(g["rolling_window_exact_ten_gate"] for g in gates.values())),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "selection20_reference_runs": list(selection_runs),
    }
    if len(result) != expected_rows:
        raise RuntimeError(f"RESULT_ROW_COUNT_MISMATCH:{len(result)}:{expected_rows}")
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"gates": {k: v["rolling_window_exact_ten_gate"] for k, v in gates.items()}, "window_count": len(windows)}, ensure_ascii=False, sort_keys=True))
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
