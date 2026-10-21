"""H440: rolling stability audit for the frozen H434 high-confidence gate.

This diagnostic reuses the H434 model family, feature set, q_side >= 0.80
gate, fee formula, slippage cells and single-position cash protocol.  It does
not search a new threshold or refit on a window's future rows.  Each
predeclared chronological window starts a fresh diagnostic $10 ledger; window
results are never concatenated and never count as Canary fills.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h434_old_clean_extratrees_high_confidence import (
    MODEL_CONFIDENCE_FLOOR,
    SLIPPAGES,
    _candidate_frame,
    _fit,
    apply_confidence_gate,
    replay_chain,
)


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
WINDOW_RUNS = 12
WINDOW_STRIDE = 4
ROBUST_WINDOW_FRACTION = 0.80


def rolling_windows(run_names: list[str], *, window_runs: int = WINDOW_RUNS,
                    stride: int = WINDOW_STRIDE) -> list[dict[str, Any]]:
    if type(window_runs) is not int or window_runs <= 0:
        raise ValueError("window_runs must be positive")
    if type(stride) is not int or stride <= 0:
        raise ValueError("stride must be positive")
    return [
        {
            "window_index": index,
            "start_run": run_names[start],
            "end_run": run_names[start + window_runs - 1],
            "runs": run_names[start:start + window_runs],
        }
        for index, start in enumerate(range(0, max(0, len(run_names) - window_runs + 1), stride), start=1)
    ]


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
        "candidate": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE",
        "slippage": float(slippage),
        "window_count": len(selected),
        "growth_positive_windows": len(passed),
        "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
        "robust_window_gate": bool(selected) and len(passed) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "all_windows_passed": bool(selected) and len(passed) == len(selected),
        "minimum_first_ten_cash_usd": min(
            (float(row["first_ten_ending_cash"]) for row in selected), default=INITIAL_CASH
        ),
        "median_first_ten_cash_usd": float(
            pd.Series([float(row["first_ten_ending_cash"]) for row in selected]).median()
        ) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min(
            (int(row["first_ten_fills"]) for row in selected), default=0
        ),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    candidates_by_slippage: dict[float, list[pd.DataFrame]] = {float(s): [] for s in SLIPPAGES}
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        if model is None:
            continue
        for slippage in SLIPPAGES:
            current = _candidate_frame(model, medians, by_run[run_name], float(slippage))
            current = apply_confidence_gate(current)
            if not current.empty:
                current = current.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)

    windows = rolling_windows(runs[4:])
    rows: list[dict[str, Any]] = []
    for window in windows:
        for slippage in SLIPPAGES:
            frames = candidates_by_slippage[float(slippage)]
            candidates = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            result = replay_chain(candidates, window["runs"])
            rows.append({
                "window_index": window["window_index"],
                "start_run": window["start_run"],
                "end_run": window["end_run"],
                "split": "h434_fixed_gate_rolling_window",
                "slippage": float(slippage),
                **_strip_chain(result),
            })
    cells = [_cell_summary(rows, float(slippage)) for slippage in SLIPPAGES]
    all_cells_robust = bool(cells) and all(bool(cell["robust_window_gate"]) for cell in cells)
    result: dict[str, Any] = {
        "format": "old-clean-h440-h434-rolling-stability-v1",
        "hypothesis_id": "H440_H434_FIXED_GATE_ROLLING_STABILITY",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "candidate": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE",
        "selection_rule": {
            "confidence_floor": MODEL_CONFIDENCE_FLOOR,
            "single_fixed_gate": True,
            "threshold_grid": False,
            "model_fit": "prequential; each scored run uses only earlier runs",
        },
        "window_count": len(windows),
        "window_runs": WINDOW_RUNS,
        "window_stride": WINDOW_STRIDE,
        "robust_window_gate": f"at least {ROBUST_WINDOW_FRACTION:.0%} of windows pass the exact ten-fill growth prefix in every slippage cell",
        "cell_summaries": cells,
        "all_cells_robust": all_cells_robust,
        "window_rows": rows,
        "decision": "paper_stability_promising_receipts_required" if all_cells_robust else "reject_h440_h434_rolling_stability",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": 5.0,
            "minimum_order_shares": 5.0,
            "taker_fee_rate": 0.07,
            "cash_release_delay_seconds": 900.0,
            "slippages": list(SLIPPAGES),
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
        "model_refit": True,
        "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean rolling paper diagnostic; windows are not Canary fills and no authenticated owner, settlement or cash-release receipt exists",
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
