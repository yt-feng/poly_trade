"""Complete H62 by replaying only the omitted H61 window/config cell.

The first H62 delivery covered 8 of H61's 9 failed cells.  This retry is
intentionally limited to ``window_1/stress`` and its three counterfactuals;
the sealed H62 v1 episode supplies the other 24 rows when results are merged.
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

from h56_rolling_window_stability import WINDOW_SIZE, WINDOW_STRIDE, rolling_windows
from h60_fast_focused_registry_verification import add_signals, prepare_fast_data
from h62_fee_slippage_counterfactual import COUNTERFACTUALS, STRATEGY, replay_counterfactual

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
MISSING_WINDOW_INDEX = 1
MISSING_CONFIG_LABEL = "stress"
BASE_H62_EPISODE = "091ff91faaf94688c4a64f7cf06848dce232c95f63c41f44fb0de50ad8936b93"
BASE_H62_RUN_ID = 39094464244


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, runs = prepare_fast_data(source_root)
    frame = add_signals(features, STRATEGY)
    windows = rolling_windows(runs[4 + 20 :])
    start, names = windows[MISSING_WINDOW_INDEX - 1]
    config = {"label": MISSING_CONFIG_LABEL, "tick_size": 0.01, "depth_floor": 150.0, "slippage": 0.01}
    rows: list[dict[str, object]] = []
    for counterfactual in COUNTERFACTUALS:
        metrics = replay_counterfactual(frame, names, config, counterfactual)
        rows.append(
            {
                "strategy": STRATEGY,
                "window_index": MISSING_WINDOW_INDEX,
                "config_label": MISSING_CONFIG_LABEL,
                "counterfactual": counterfactual["label"],
                "window_start_offset": start,
                "window_end_offset_exclusive": start + WINDOW_SIZE,
                "window_start_run": names[0],
                "window_end_run": names[-1],
                **metrics,
            }
        )
    result = pd.DataFrame(rows)
    if len(result) != len(COUNTERFACTUALS):
        raise RuntimeError(f"RESULT_ROW_COUNT_MISMATCH:{len(result)}:{len(COUNTERFACTUALS)}")
    summary = {
        "format": "old-clean-h62-missing-cell-retry1-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "strategy": STRATEGY,
        "missing_cell": {"window_index": MISSING_WINDOW_INDEX, "config_label": MISSING_CONFIG_LABEL},
        "counterfactuals": list(COUNTERFACTUALS),
        "base_h62_episode_id": BASE_H62_EPISODE,
        "base_h62_run_id": BASE_H62_RUN_ID,
        "base_rows_reused": 24,
        "new_rows": len(result),
        "combined_rows_expected": 27,
        "protocol": {
            "same_ledger_per_window": True,
            "required_fills": 10,
            "window_size_runs": WINDOW_SIZE,
            "window_stride_runs": WINDOW_STRIDE,
            "selection_repeated": False,
            "promotion_allowed": False,
            "new_data_collected": False,
        },
        "rows": result.to_dict(orient="records"),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "counterfactual_metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(result), "missing_cell": summary["missing_cell"]}, sort_keys=True))
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
