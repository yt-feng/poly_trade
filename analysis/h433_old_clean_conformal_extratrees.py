"""Pre-registered non-stationary conformal gate on the old-clean ExtraTrees model.

H432 showed that an ExtraTrees challenger could look excellent on a pooled
holdout while its first selection trade lost.  H433 keeps the H432 model and
execution contract fixed, then adds one causal online calibration gate: the
current run may use only nonconformity scores from predictions made on earlier
runs.  No current-run outcome, future run, threshold grid, or reset ledger is
allowed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    FEATURES,
    INITIAL_CASH,
    MODEL_PARAMS,
    REQUIRED_FILLS,
    SLIPPAGES,
    _candidate_frame,
    _fit,
    replay_chain,
)


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
ALPHA = 0.10
CALIBRATION_RUNS = 20
MIN_CALIBRATION_ROWS = 200
MIN_CONFIDENCE = 0.50

LITERATURE = {
    "online_model_selection": {
        "title": "Online Conformal Model Selection for Nonstationary Time Series",
        "url": "https://arxiv.org/abs/2506.05544",
        "retrieved_utc": "2026-10-21",
        "use": "recency-aware calibration design reference; no paper results imported",
    },
    "official_reference_implementation": {
        "title": "salesforce/online_conformal",
        "url": "https://github.com/salesforce/online_conformal",
        "retrieved_utc": "2026-10-21",
        "use": "online conformal state/update reference; no external package dependency",
    },
    "time_series_benchmark": {
        "title": "Conformal Prediction Algorithms for Time Series Forecasting: Methods and Benchmark",
        "url": "https://arxiv.org/abs/2601.18509",
        "retrieved_utc": "2026-10-21",
        "use": "benchmark design reference only",
    },
}


def _nonconformity(q_up: np.ndarray, outcome_up: np.ndarray) -> np.ndarray:
    """Binary probability nonconformity: one minus probability of true class."""
    return np.where(outcome_up.astype(int) == 1, 1.0 - q_up, q_up)


def conformal_threshold(scores: np.ndarray, *, alpha: float = ALPHA) -> float | None:
    scores = np.asarray(scores, dtype=float)
    scores = scores[np.isfinite(scores)]
    if len(scores) < MIN_CALIBRATION_ROWS:
        return None
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between zero and one")
    # ``higher`` is conservative at the finite-sample quantile.  Requiring
    # q_side >= 1-q makes the accepted class part of the calibrated set; the
    # 0.50 floor prevents a direction with less than coin-flip probability.
    quantile = float(np.quantile(scores, 1.0 - alpha, method="higher"))
    return max(MIN_CONFIDENCE, min(1.0, 1.0 - quantile))


def _calibrated_candidates(model, medians, current: pd.DataFrame, threshold: float, slippage: float) -> pd.DataFrame:
    scored = _candidate_frame(model, medians, current, slippage)
    if scored.empty:
        return scored
    return scored[scored["q_side"].ge(float(threshold))].copy()


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    features, _coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}

    # Each entry is the prediction made for that complete run by a model fit
    # only before the run.  Calibration for run i is drawn from earlier entries.
    prediction_by_run: dict[str, pd.DataFrame] = {}
    candidates_by_slippage: dict[float, list[pd.DataFrame]] = {float(s): [] for s in SLIPPAGES}
    calibration_trace: list[dict[str, Any]] = []
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        if model is None:
            continue
        current = by_run[run_name]
        q_up = model.predict_proba(current[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(medians).fillna(0.0))[:, 1]
        current_predictions = pd.DataFrame({"q_up": q_up, "outcome_up": pd.to_numeric(current["outcome_up"], errors="coerce").to_numpy()})
        historical_runs = [name for name in runs[:index] if name in prediction_by_run][-CALIBRATION_RUNS:]
        historical = pd.concat([prediction_by_run[name] for name in historical_runs], ignore_index=True) if historical_runs else pd.DataFrame()
        scores = _nonconformity(historical["q_up"].to_numpy(), historical["outcome_up"].to_numpy()) if not historical.empty else np.array([], dtype=float)
        threshold = conformal_threshold(scores)
        calibration_trace.append({"run_name": str(run_name), "model_run_index": index, "calibration_runs": len(historical_runs), "calibration_rows": int(len(scores)), "threshold": threshold})
        if threshold is not None:
            for slippage in SLIPPAGES:
                candidate = _calibrated_candidates(model, medians, current, threshold, float(slippage))
                if not candidate.empty:
                    candidate = candidate.copy()
                    candidate["conformal_threshold"] = float(threshold)
                    candidate["model_run_index"] = index
                    candidates_by_slippage[float(slippage)].append(candidate)
        prediction_by_run[str(run_name)] = current_predictions

    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    holdout_blocks = [holdout_runs[i:i + 12] for i in range(0, len(holdout_runs), 12)]
    results: dict[str, Any] = {}
    for slippage in SLIPPAGES:
        frames = candidates_by_slippage[float(slippage)]
        candidates = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        results[f"{slippage:.3f}"] = {
            "selection20": replay_chain(candidates, selection_runs),
            "holdout59": replay_chain(candidates, holdout_runs),
            "all_test79": replay_chain(candidates, test_runs),
            "holdout_blocks": [replay_chain(candidates, block) for block in holdout_blocks],
        }

    def strip_chain(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip_chain(item) for key, item in value.items() if key != "chain"}
        if isinstance(value, list):
            return [strip_chain(item) for item in value]
        return value

    trace_thresholds = [float(item["threshold"]) for item in calibration_trace if item["threshold"] is not None]
    report: dict[str, Any] = {
        "format": "old-clean-h433-online-conformal-extratrees-strict-compound-v1",
        "hypothesis_id": "H433_ONLINE_CONFORMAL_GATE_ON_PREQUENTIAL_EXTRATREES",
        "source_commit": source_commit,
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "model": {"kind": "ExtraTreesClassifier", "features": FEATURES, "params": MODEL_PARAMS, "training_runs_strictly_before_current_run": True},
        "conformal_gate": {"alpha": ALPHA, "calibration_runs": CALIBRATION_RUNS, "minimum_calibration_rows": MIN_CALIBRATION_ROWS, "quantile_method": "higher", "nonconformity": "1 - probability_of_true_class", "minimum_confidence": MIN_CONFIDENCE, "current_run_outcome_excluded": True, "threshold_search": False},
        "calibration_trace": calibration_trace,
        "calibration_threshold_summary": {"usable_runs": len(trace_thresholds), "minimum": min(trace_thresholds) if trace_thresholds else None, "median": float(np.median(trace_thresholds)) if trace_thresholds else None, "maximum": max(trace_thresholds) if trace_thresholds else None},
        "execution_contract": {"initial_cash_usd": INITIAL_CASH, "minimum_order_usd": 5.0, "minimum_order_shares": 5.0, "taker_fee_rate": 0.07, "cash_release_delay_seconds": 900.0, "single_position_only": True, "prior_cash_release_required": True, "same_ledger": True, "no_reset": True, "no_concatenation": True, "required_consecutive_fills": REQUIRED_FILLS, "paper_only": True},
        "literature": LITERATURE,
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "slippages": {key: strip_chain(value) for key, value in results.items()},
        "evidence_boundary": "strict-quality pinned old-clean paper replay; no authenticated order, fill, settlement, released-cash, or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"format": report["format"], "feature_rows": report["feature_rows"], "calibration_threshold_summary": report["calibration_threshold_summary"], "slippages": report["slippages"]}, ensure_ascii=False, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
