"""H601: combine favorite-side alignment, path efficiency and H578 depth.

H600 retained the H434 high-confidence signal, required the selected side's
two-minute ask to be at least 0.50, and used the H578 entry-depth map.  Its
weakest stress cells were unchanged from H578.  H601 adds one causal,
entry-visible state filter: the first-two-minute price path must have
``path_efficiency_first2m >= 0.50``.  The intent is to remove choppy entry
paths that can consume depth without changing the cash-ledger protocol.

This is one pre-registered paper policy on the pinned strict-quality archive.
It does not use post-entry fields, collect data, place orders, or create
authenticated cash-release evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from . import h561_online_depth_proxy as base
except ImportError:
    import h561_online_depth_proxy as base


HYPOTHESIS_ID = "H601_FAVORITE_PATH_EFFICIENCY_H578_DEPTH"
RESULT_FORMAT = "old-clean-h601-favorite-path-depth-v1"
SOURCE_COMMIT = base.SOURCE_COMMIT
THRESHOLDS = (3.0, 5.0, 7.0)
FAVORITE_PRICE_FLOOR = 0.50
PATH_EFFICIENCY_FLOOR = 0.50


def favorite_path_only(candidates: pd.DataFrame) -> pd.DataFrame:
    """Keep only entry-visible favorite-side price and path-quality rows."""
    if candidates.empty:
        return candidates.copy()
    price = pd.to_numeric(candidates["price"], errors="coerce")
    path = pd.to_numeric(candidates["path_efficiency_first2m"], errors="coerce")
    return candidates.loc[
        price.ge(FAVORITE_PRICE_FLOOR) & path.ge(PATH_EFFICIENCY_FLOOR)
    ].copy()


def _summary(frame: pd.DataFrame, split: str) -> dict[str, Any]:
    if frame.empty:
        return {
            "split": split,
            "rows": 0,
            "passed_rows": 0,
            "all_rows_passed": False,
            "min_fills": 0,
            "min_cash": base.INITIAL_CASH,
            "median_cash": base.INITIAL_CASH,
        }
    passed = frame["first_ten_passed"].astype(bool)
    return {
        "split": split,
        "rows": int(len(frame)),
        "passed_rows": int(passed.sum()),
        "all_rows_passed": bool(passed.all()),
        "min_fills": int(frame["first_ten_fills"].min()),
        "min_cash": float(frame["first_ten_ending_cash"].min()),
        "median_cash": float(frame["first_ten_ending_cash"].median()),
    }


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    by_slippage, runs, feature_rows = base._candidate_frames(source_root)
    all_candidates = pd.concat(
        [frame for frames in by_slippage.values() for frame in frames], ignore_index=True
    )
    if "path_efficiency_first2m" not in all_candidates.columns:
        raise RuntimeError("PATH_EFFICIENCY_FIELD_MISSING")
    quotes = base._quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = base.rolling_windows(
        runs[4:], window_runs=base.WINDOW_RUNS, stride=base.WINDOW_STRIDE
    )
    rows: list[dict[str, Any]] = []
    for slippage in base.SLIPPAGES:
        frames = by_slippage[float(slippage)]
        candidate = favorite_path_only(
            pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        )
        for latency in base.LATENCIES_SECONDS:
            for haircut in base.DEPTH_HAIRCUTS:
                cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                for window in windows:
                    result = base.replay(
                        candidate,
                        window["runs"],
                        quotes,
                        latency_seconds=latency,
                        depth_haircut=haircut,
                        exit_cache=cache,
                        thresholds=THRESHOLDS,
                    )
                    rows.append(
                        {
                            "slippage": float(slippage),
                            "latency_seconds": float(latency),
                            "depth_haircut": float(haircut),
                            "window_index": int(window["window_index"]),
                            **{key: value for key, value in result.items() if key != "chain"},
                        }
                    )

    frame = pd.DataFrame(rows)
    selection = frame[frame["window_index"] <= base.SELECTION_WINDOWS]
    holdout = frame[frame["window_index"] > base.SELECTION_WINDOWS]
    selection_summary = _summary(selection, "selection")
    holdout_summary = _summary(holdout, "holdout")
    result: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": feature_rows,
        "rule": {
            "base_signal": "H434 q_side >= 0.80 ExtraTrees candidate",
            "favorite_side_definition": "selected side two-minute ask >= 0.50",
            "favorite_price_floor": FAVORITE_PRICE_FLOOR,
            "path_efficiency_definition": "path_efficiency_first2m >= 0.50",
            "path_efficiency_floor": PATH_EFFICIENCY_FLOOR,
            "depth_thresholds": list(THRESHOLDS),
            "selection_stress_cells": 24,
            "holdout_stress_cells": 12,
            "no_threshold_grid": True,
        },
        "selection": selection_summary,
        "holdout": holdout_summary,
        "all_408_stress_rows_passed": bool(
            selection_summary["all_rows_passed"] and holdout_summary["all_rows_passed"]
        ),
        "decision": "retain_only_if_selection_and_holdout_stress_gates_pass",
        "protocol": {
            "initial_cash_usd": base.INITIAL_CASH,
            "required_fills": base.REQUIRED_FILLS,
            "same_ledger": True,
            "prior_cash_release_required": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "sources": [
            {
                "title": "The Anatomy of a Decentralized Prediction Market: Microstructure Evidence from the Polymarket Order Book",
                "url": "https://arxiv.org/abs/2604.24366",
                "use": "keep entry-visible filters separate from noisy inferred trade direction",
            },
            {
                "title": "polybtc research report",
                "url": "https://github.com/LukasNSteel/polybtc/blob/main/research/REPORT.md",
                "use": "favorite-side hypothesis is transferred only through a preregistered old-clean test",
            },
        ],
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "strict-quality old-clean quote replay; no authenticated order, fill, settlement, released-cash, or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output / "rolling-metrics.csv", index=False)
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n"
    result["summary_sha256"] = hashlib.sha256(payload).hexdigest()
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps({"selection": result["selection"], "holdout": result["holdout"]}, sort_keys=True))


if __name__ == "__main__":
    main()
