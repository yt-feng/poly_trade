"""Pre-registered high-confidence ExtraTrees compound replay.

H432/H433 exposed an early selection loss from a low-confidence model choice.
H434 freezes one economic selection rule, q_side >= 0.80, on the same causal
ExtraTrees predictions and the same strict cash-release contract.  It is a
single gate, not a threshold grid.  The cloud run is the validation record;
the local exploratory result is not promoted by itself.
"""
from __future__ import annotations

import argparse
import json
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
MODEL_CONFIDENCE_FLOOR = 0.80


def apply_confidence_gate(candidates: pd.DataFrame, floor: float = MODEL_CONFIDENCE_FLOOR) -> pd.DataFrame:
    if not 0.5 <= float(floor) < 1.0:
        raise ValueError("confidence floor must be in [0.5, 1.0)")
    if candidates.empty:
        return candidates.copy()
    return candidates[candidates["q_side"].ge(float(floor))].copy()


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    features, _coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
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
        current = by_run[run_name]
        for slippage in SLIPPAGES:
            candidate = _candidate_frame(model, medians, current, float(slippage))
            candidate = apply_confidence_gate(candidate)
            if not candidate.empty:
                candidate = candidate.copy()
                candidate["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(candidate)

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

    report: dict[str, Any] = {
        "format": "old-clean-h434-extratrees-high-confidence-strict-compound-v1",
        "hypothesis_id": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE",
        "source_commit": source_commit,
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "model": {"kind": "ExtraTreesClassifier", "features": FEATURES, "params": MODEL_PARAMS, "training_runs_strictly_before_current_run": True},
        "selection_rule": {"model_confidence_definition": "max(q_up, 1-q_up)", "model_confidence_floor": MODEL_CONFIDENCE_FLOOR, "single_fixed_gate": True, "threshold_grid": False, "fixed_slippages": list(SLIPPAGES)},
        "execution_contract": {"initial_cash_usd": INITIAL_CASH, "minimum_order_usd": 5.0, "minimum_order_shares": 5.0, "taker_fee_rate": 0.07, "cash_release_delay_seconds": 900.0, "single_position_only": True, "prior_cash_release_required": True, "same_ledger": True, "no_reset": True, "no_concatenation": True, "required_consecutive_fills": REQUIRED_FILLS, "paper_only": True},
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
    print(json.dumps({"format": report["format"], "feature_rows": report["feature_rows"], "selection_rule": report["selection_rule"], "slippages": report["slippages"]}, ensure_ascii=False, sort_keys=True))
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
