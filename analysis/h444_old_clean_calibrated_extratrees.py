"""H444: causal probability calibration on the frozen H434 signal.

H441 found that the fixed q_side >= 0.80 ExtraTrees gate is paper-stable in
16/17 rolling windows, but one window exhausts a $10 ledger after consecutive
losses.  H444 tests one predeclared mechanism for that failure: fit a Platt
logistic calibrator only on earlier out-of-sample predictions, then apply a
fixed calibrated-confidence floor of 0.80 to the current run.  The model
features, execution costs, release clock and continuous ten-fill ledger stay
unchanged.  This is a paper diagnostic, never a live-order path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    EDGE_THRESHOLD,
    FEATURES,
    INITIAL_CASH,
    MAX_OVERROUND,
    MAX_SPREAD,
    MIN_DEPTH,
    MINIMUM_ORDER_SHARES,
    MINIMUM_ORDER_USD,
    MODEL_PARAMS,
    PRICE_MAX,
    PRICE_MIN,
    REQUIRED_FILLS,
    SLIPPAGES,
    TAKER_FEE_RATE,
    _fit,
    _numeric,
)
from h440_h434_rolling_stability import rolling_windows
from h441_h434_loss_tolerant_compound import replay_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
ENTRY_SECONDS_AFTER_START = 124.0
CASH_RELEASE_SECONDS = 900.0
CALIBRATION_RUNS = 20
MIN_CALIBRATION_ROWS = 200
CALIBRATED_CONFIDENCE_FLOOR = 0.80
WINDOW_RUNS = 12
WINDOW_STRIDE = 4
ROBUST_WINDOW_FRACTION = 0.80
BASELINE_PASSED_WINDOWS = 16


def _logit(values: Any) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    return np.log(np.clip(values, 1e-6, 1.0 - 1e-6) / np.clip(1.0 - values, 1e-6, 1.0))


def fit_platt_calibrator(history: pd.DataFrame) -> LogisticRegression | None:
    """Fit only when historical out-of-sample rows are sufficient and binary."""
    if history.empty or len(history) < MIN_CALIBRATION_ROWS:
        return None
    required = {"q_up", "outcome_up"}
    if not required.issubset(history.columns):
        return None
    q = pd.to_numeric(history["q_up"], errors="coerce").to_numpy(dtype=float)
    y = pd.to_numeric(history["outcome_up"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(q) & np.isfinite(y) & np.isin(y, [0.0, 1.0])
    q, y = q[valid], y[valid]
    if len(q) < MIN_CALIBRATION_ROWS or np.unique(y).size != 2:
        return None
    calibrator = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
    calibrator.fit(_logit(q).reshape(-1, 1), y.astype(int))
    return calibrator


def calibrated_probability(calibrator: LogisticRegression, q_up: Any) -> np.ndarray:
    if calibrator is None:
        raise ValueError("calibrator is required")
    return calibrator.predict_proba(_logit(q_up).reshape(-1, 1))[:, 1]


def _scored_candidates(
    model: Any,
    medians: pd.Series,
    current: pd.DataFrame,
    slippage: float,
    calibrator: LogisticRegression,
) -> pd.DataFrame:
    if model is None or current.empty or calibrator is None:
        return pd.DataFrame()
    scored = current.copy()
    raw_q_up = model.predict_proba(_numeric(scored, FEATURES, medians))[:, 1]
    scored["q_up_raw"] = raw_q_up
    scored["q_up"] = calibrated_probability(calibrator, raw_q_up)
    scored["q_side"] = np.where(scored["q_up"] >= 0.5, scored["q_up"], 1.0 - scored["q_up"])
    scored["side"] = np.where(scored["q_up"] >= 0.5, "up", "down")
    scored["price"] = np.where(scored["side"].eq("up"), scored["buy_up_price_2m"], scored["buy_down_price_2m"])
    scored["depth"] = np.where(scored["side"].eq("up"), scored["buy_up_size_2m"], scored["buy_down_size_2m"])
    scored["spread"] = np.where(scored["side"].eq("up"), scored["spread_up_median_first2m"], scored["spread_down_median_first2m"])
    scored["price"] = pd.to_numeric(scored["price"], errors="coerce")
    scored["fill_price"] = scored["price"] + float(slippage)
    scored["shares"] = np.maximum(MINIMUM_ORDER_SHARES, MINIMUM_ORDER_USD / scored["fill_price"])
    scored["cost"] = scored["shares"] * scored["fill_price"]
    scored["fee"] = scored["shares"] * TAKER_FEE_RATE * scored["fill_price"] * (1.0 - scored["fill_price"])
    scored["edge"] = scored["q_side"] - scored["price"] - TAKER_FEE_RATE * scored["fill_price"] * (1.0 - scored["fill_price"])
    eligible = (
        scored["q_side"].ge(CALIBRATED_CONFIDENCE_FLOOR)
        & scored["edge"].ge(EDGE_THRESHOLD)
        & scored["price"].between(PRICE_MIN, PRICE_MAX)
        & pd.to_numeric(scored["depth"], errors="coerce").ge(MIN_DEPTH)
        & pd.to_numeric(scored["spread"], errors="coerce").le(MAX_SPREAD)
        & pd.to_numeric(scored["overround_median_first2m"], errors="coerce").le(MAX_OVERROUND)
        & scored["fill_price"].lt(0.98)
        & (scored["cost"] + scored["fee"]).le(INITIAL_CASH)
    )
    scored = scored.loc[eligible].copy()
    if scored.empty:
        return scored
    scored["entry_ts"] = pd.to_datetime(scored["first_quote_ts"], utc=True) + pd.Timedelta(seconds=ENTRY_SECONDS_AFTER_START)
    epoch = pd.to_numeric(scored["slug"].astype(str).str.rsplit("-", n=1).str[-1], errors="coerce")
    scored["settlement_ts"] = pd.to_datetime(epoch, unit="s", utc=True, errors="coerce")
    scored["cash_release_ts"] = scored["settlement_ts"] + pd.Timedelta(seconds=CASH_RELEASE_SECONDS)
    scored["slippage"] = float(slippage)
    return scored.sort_values(["entry_ts", "slug"], kind="mergesort")


def _strip_chain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_chain(item) for key, item in value.items() if key != "chain"}
    if isinstance(value, list):
        return [_strip_chain(item) for item in value]
    return value


def _cell_summary(rows: list[dict[str, Any]], slippage: float) -> dict[str, Any]:
    selected = [row for row in rows if float(row["slippage"]) == float(slippage)]
    passed = [row for row in selected if bool(row["first_ten_passed"])]
    return {
        "candidate": "H444_H434_PREQUENTIAL_PLATT_CALIBRATION",
        "slippage": float(slippage),
        "window_count": len(selected),
        "growth_positive_windows": len(passed),
        "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
        "robust_window_gate": bool(selected) and len(passed) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "all_windows_passed": bool(selected) and len(passed) == len(selected),
        "minimum_first_ten_cash_usd": min((float(row["first_ten_ending_cash"]) for row in selected), default=INITIAL_CASH),
        "median_first_ten_cash_usd": float(pd.Series([float(row["first_ten_ending_cash"]) for row in selected]).median()) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
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
        raw_q_up = model.predict_proba(_numeric(current, FEATURES, medians))[:, 1]
        current_predictions = pd.DataFrame({"q_up": raw_q_up, "outcome_up": pd.to_numeric(current["outcome_up"], errors="coerce").to_numpy()})
        historical_runs = [name for name in runs[:index] if name in prediction_by_run][-CALIBRATION_RUNS:]
        historical = pd.concat([prediction_by_run[name] for name in historical_runs], ignore_index=True) if historical_runs else pd.DataFrame()
        calibrator = fit_platt_calibrator(historical)
        calibration_trace.append({"run_name": str(run_name), "model_run_index": index, "calibration_runs": len(historical_runs), "calibration_rows": int(len(historical)), "calibrator_available": calibrator is not None})
        if calibrator is not None:
            for slippage in SLIPPAGES:
                candidate = _scored_candidates(model, medians, current, float(slippage), calibrator)
                if not candidate.empty:
                    candidate = candidate.copy()
                    candidate["model_run_index"] = index
                    candidate["calibrated_confidence_floor"] = CALIBRATED_CONFIDENCE_FLOOR
                    candidates_by_slippage[float(slippage)].append(candidate)
        prediction_by_run[str(run_name)] = current_predictions

    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    for window in windows:
        for slippage in SLIPPAGES:
            frames = candidates_by_slippage[float(slippage)]
            candidates = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            rows.append({
                "window_index": window["window_index"], "start_run": window["start_run"], "end_run": window["end_run"],
                "split": "h444_h434_prequential_platt_calibration_rolling_window", "slippage": float(slippage),
                **_strip_chain(replay_chain(candidates, window["runs"])),
            })
    cells = [_cell_summary(rows, float(slippage)) for slippage in SLIPPAGES]
    all_cells_robust = bool(cells) and all(bool(cell["robust_window_gate"]) for cell in cells)
    non_degrading_vs_h441 = bool(cells) and all(
        int(cell["growth_positive_windows"]) >= BASELINE_PASSED_WINDOWS for cell in cells
    )
    result: dict[str, Any] = {
        "format": "old-clean-h444-h434-prequential-platt-calibration-v1",
        "hypothesis_id": "H444_H434_PREQUENTIAL_PLATT_CALIBRATION",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "strict_quality": True, "complete_runs": len(runs), "feature_rows": int(len(features)),
        "candidate": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE_WITH_PLATT_CALIBRATION",
        "selection_rule": {
            "calibration": "Platt logistic map fit only on earlier runs' out-of-sample predictions",
            "calibration_runs": CALIBRATION_RUNS, "minimum_calibration_rows": MIN_CALIBRATION_ROWS,
            "calibrated_confidence_floor": CALIBRATED_CONFIDENCE_FLOOR, "threshold_grid": False,
            "model_fit": "prequential; each scored run uses only earlier runs",
        },
        "calibration_trace": calibration_trace,
        "window_count": len(windows), "window_runs": WINDOW_RUNS, "window_stride": WINDOW_STRIDE,
        "cell_summaries": cells, "all_cells_robust": all_cells_robust,
        "baseline_h441_passed_windows_per_slippage": BASELINE_PASSED_WINDOWS,
        "non_degrading_vs_h441": non_degrading_vs_h441,
        "window_rows": rows,
        "decision": "paper_stability_supports_calibration_receipts_required" if all_cells_robust and non_degrading_vs_h441 else "reject_h444_calibration_stability_or_no_improvement",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "required_fills": REQUIRED_FILLS, "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": MINIMUM_ORDER_SHARES, "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_SECONDS, "slippages": list(SLIPPAGES),
            "same_ledger_within_window": True, "settled_loss_continues_if_next_order_is_fundable": True,
            "first_ten_pass_requires_net_growth_not_all_wins": True, "fresh_diagnostic_ledger_per_window": True,
            "no_cross_window_concatenation": True, "old_clean_dataset_only": True, "paper_only": True,
        },
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": False, "model_refit": True, "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean rolling paper diagnostic; no authenticated owner, settlement or cash-release receipt exists",
        "root_cause": "H441's weak window may reflect overconfident prequential probabilities; H444 tests causal calibration without changing the model family or execution contract",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"window_count": len(windows), "cell_summaries": cells, "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
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
