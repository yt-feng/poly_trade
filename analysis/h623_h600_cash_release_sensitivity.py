"""H623: measure H600's cash-release-clock sensitivity on old-clean data.

H600's signal, favorite-side floor, depth map, fee, slippage, exit stress and
one evolving $10 ledger stay fixed.  Only the counterfactual cash-release
clock changes.  The 900-second cell is the frozen protocol; 300/600 seconds
are diagnostics for whether five-minute turnover is the binding constraint.
No paper result can change the authenticated Canary gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
import sys

if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h600_favorite_side_depth as h600


HYPOTHESIS_ID = "H623_H600_CASH_RELEASE_CLOCK_SENSITIVITY"
RESULT_FORMAT = "old-clean-h623-h600-cash-release-sensitivity-v1"
SOURCE_COMMIT = h600.SOURCE_COMMIT
DELAYS_SECONDS = (300.0, 600.0, 900.0)
THRESHOLDS = h600.THRESHOLDS


def shift_release_times(frame: pd.DataFrame, delay_seconds: float) -> pd.DataFrame:
    """Return a copy with only the counterfactual release timestamp changed."""
    delay = float(delay_seconds)
    if delay not in DELAYS_SECONDS:
        raise ValueError(f"unsupported release delay: {delay_seconds}")
    out = frame.copy()
    settlement = pd.to_datetime(out["settlement_ts"], utc=True, errors="coerce")
    if settlement.isna().any():
        raise ValueError("settlement_ts contains an invalid timestamp")
    out["cash_release_ts"] = settlement + pd.to_timedelta(delay, unit="s")
    return out


def _summary(frame: pd.DataFrame, split: str, delay: float) -> dict[str, Any]:
    if frame.empty:
        return {
            "split": split,
            "delay_seconds": float(delay),
            "rows": 0,
            "passed_rows": 0,
            "all_rows_passed": False,
            "min_fills": 0,
            "min_cash": 10.0,
            "median_cash": 10.0,
            "blocked_before_release": 0,
        }
    passed = frame["first_ten_passed"].astype(bool)
    return {
        "split": split,
        "delay_seconds": float(delay),
        "rows": int(len(frame)),
        "passed_rows": int(passed.sum()),
        "all_rows_passed": bool(passed.all()),
        "min_fills": int(frame["first_ten_fills"].min()),
        "min_cash": float(frame["first_ten_ending_cash"].min()),
        "median_cash": float(frame["first_ten_ending_cash"].median()),
        "blocked_before_release": int(frame["blocked_before_release"].sum()),
    }


def evaluate(
    source_root: Path,
    *,
    source_commit: str = SOURCE_COMMIT,
    output: Path,
) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    # H600 delegates the prequential candidate construction to H561's shared
    # implementation; keep that exact source/model path instead of recreating it.
    by_slippage, runs, feature_rows = h600.base._candidate_frames(source_root)
    all_candidates = pd.concat(
        [frame for frames in by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = h600.base._quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = h600.base.rolling_windows(
        runs[4:], window_runs=h600.base.WINDOW_RUNS, stride=h600.base.WINDOW_STRIDE
    )
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    for delay in DELAYS_SECONDS:
        for slippage in h600.base.SLIPPAGES:
            frames = by_slippage[float(slippage)]
            raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            candidate = h600.favorite_side_only(raw)
            candidate = shift_release_times(candidate, delay)
            for latency in h600.base.LATENCIES_SECONDS:
                for haircut in h600.base.DEPTH_HAIRCUTS:
                    cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                    for window in windows:
                        result = h600.base.replay(
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
                                "delay_seconds": float(delay),
                                "slippage": float(slippage),
                                "latency_seconds": float(latency),
                                "depth_haircut": float(haircut),
                                "window_index": int(window["window_index"]),
                                **{key: value for key, value in result.items() if key != "chain"},
                            }
                        )

    table = pd.DataFrame(rows)
    for delay in DELAYS_SECONDS:
        scoped = table[table["delay_seconds"].eq(float(delay))]
        selection = scoped[scoped["window_index"] <= h600.base.SELECTION_WINDOWS]
        holdout = scoped[scoped["window_index"] > h600.base.SELECTION_WINDOWS]
        summaries.extend(
            [_summary(selection, "selection", delay), _summary(holdout, "holdout", delay)]
        )

    by_delay = {
        str(delay): {
            "selection": next(item for item in summaries if item["delay_seconds"] == delay and item["split"] == "selection"),
            "holdout": next(item for item in summaries if item["delay_seconds"] == delay and item["split"] == "holdout"),
        }
        for delay in DELAYS_SECONDS
    }
    all_delays_pass = all(
        bool(value["selection"]["all_rows_passed"] and value["holdout"]["all_rows_passed"])
        for value in by_delay.values()
    )
    result: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": feature_rows,
        "frozen_route": "H600_FAVORITE_SIDE_ALIGNMENT_H578_DEPTH",
        "rule": {
            "favorite_side_floor": h600.FAVORITE_PRICE_FLOOR,
            "depth_thresholds": list(THRESHOLDS),
            "delays_seconds": list(DELAYS_SECONDS),
            "frozen_protocol_delay_seconds": 900.0,
            "only_release_clock_changes": True,
        },
        "selection": [value["selection"] for value in by_delay.values()],
        "holdout": [value["holdout"] for value in by_delay.values()],
        "by_delay": by_delay,
        "all_delays_selection_and_holdout_pass": all_delays_pass,
        "decision": (
            "release_clock_not_binding_for_h600_paper_chain"
            if all_delays_pass
            else "release_clock_sensitivity_does_not_repair_or_generalize_h600"
        ),
        "protocol": {
            "initial_cash_usd": 10.0,
            "minimum_order_usd": 5.0,
            "taker_fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "required_fills": 10,
            "same_ledger": True,
            "prior_cash_release_required": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "strict-quality old-clean paper replay with counterfactual cash clock; no authenticated order, fill, settlement, release or balance receipt",
        "root_cause": "tests whether the five-minute turnover limitation is the release clock after the H600 entry and depth policy are frozen",
    }
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "rolling-metrics.csv", index=False)
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n"
    result["summary_sha256"] = hashlib.sha256(payload).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps({"selection": result["selection"], "holdout": result["holdout"], "decision": result["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()

