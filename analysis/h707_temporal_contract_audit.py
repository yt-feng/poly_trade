"""H707: audit the frozen paper frontier's temporal information boundary.

This is an audit only.  It deliberately does not fit a model, search a
parameter, collect market data, or submit an order.  It loads the pinned
old-clean source in the cloud and checks whether every feature used by the
frozen H432/H534 path could have been observed by its declared entry time.
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

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h432_old_clean_extratrees_compound as frozen
import v1_execution_walk_forward as study


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
HYPOTHESIS_ID = "H707_QML_TEMPORAL_CAUSAL_BOUNDARY_AUDIT"
RESULT_FORMAT = "old-clean-h707-temporal-contract-audit-v1"
DECISION_OFFSET_SECONDS = float(frozen.ENTRY_SECONDS_AFTER_START)
MARKET_DURATION_SECONDS = 300.0

# The feature builder's snapshot ages are measured backwards from these
# declared cutoffs.  The map is intentionally explicit so a newly added
# feature cannot silently inherit a permissive timestamp.
FEATURE_CUTOFF_SECONDS = {
    "btc_move_1m": 60.0,
    "btc_move_2m": 120.0,
    "btc_move_4m": 240.0,
    "mid_up_prob_open": 0.0,
    "mid_up_prob_2m": 120.0,
    "mid_up_prob_change_2m": 120.0,
}
for _name in (
    "buy_up_price_2m", "buy_down_price_2m", "buy_up_size_2m",
    "buy_down_size_2m", "sell_up_size_2m", "sell_down_size_2m",
    "size_imbalance_updown_2m", "sell_size_imbalance_updown_2m",
    "bid_depth_imbalance_updown_2m", "book_pressure_up_2m",
    "book_pressure_down_2m", "spread_up_median_first2m",
    "spread_down_median_first2m", "overround_median_first2m",
    "quote_count_first2m", "trade_count_sum_first2m",
    "trade_volume_sum_first2m", "realized_vol_first2m",
    "path_efficiency_first2m", "max_drawdown_first2m", "max_rebound_first2m",
):
    FEATURE_CUTOFF_SECONDS[_name] = 120.0


def _timestamp(value: Any) -> pd.Timestamp:
    return pd.to_datetime(value, utc=True, errors="coerce")


def _feature_timestamp(features: pd.DataFrame, name: str) -> pd.Series:
    """Return the observed timestamp for a feature family."""
    first = pd.to_datetime(features["first_quote_ts"], utc=True, errors="coerce")
    if name == "mid_up_prob_open":
        return first
    if name.startswith("btc_move_"):
        minute = int(name.split("_")[-1][:-1])
        age = pd.to_numeric(features[f"snapshot_age_seconds_{minute}m"], errors="coerce")
        return first + pd.to_timedelta(float(minute * 60), unit="s") - pd.to_timedelta(age, unit="s")
    # All other declared features are derived from the 2-minute snapshot or
    # the first two-minute prefix; the last usable observation is no later
    # than the 2-minute cutoff minus the recorded snapshot age.
    age = pd.to_numeric(features["snapshot_age_seconds_2m"], errors="coerce")
    return first + pd.to_timedelta(120.0, unit="s") - pd.to_timedelta(age, unit="s")


def _label_timestamp(features: pd.DataFrame) -> pd.Series:
    """Return the label-availability boundary, not the slug's start time."""
    start = features["slug"].astype(str).map(study.base.parse_close_ts_from_slug)
    return start + pd.to_timedelta(MARKET_DURATION_SECONDS, unit="s")


def _identity_hash(features: pd.DataFrame) -> str:
    columns = [
        "market_id", "run_name", "slug", "first_quote_ts",
        "snapshot_age_seconds_1m", "snapshot_age_seconds_2m",
        "snapshot_age_seconds_4m", "outcome_up",
    ]
    frame = features[columns].copy()
    for column in frame.columns:
        if column.endswith("_ts"):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce").astype("string")
    payload = frame.sort_values(["first_quote_ts", "market_id"], kind="mergesort").to_json(
        orient="records", date_format="iso", double_precision=15
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _run_boundary_audit(features: pd.DataFrame, runs: list[str]) -> dict[str, Any]:
    """Check that labels from training runs precede each scored run's decisions."""
    first = pd.to_datetime(features["first_quote_ts"], utc=True, errors="coerce")
    decision = first + pd.to_timedelta(DECISION_OFFSET_SECONDS, unit="s")
    close = _label_timestamp(features)
    violations: list[dict[str, Any]] = []
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        current = features[features["run_name"].astype(str).eq(str(run_name))]
        if current.empty:
            continue
        cutoff = decision.loc[current.index].min()
        prior = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        prior_labels = close.loc[prior.index].dropna()
        if not prior_labels.empty and prior_labels.max() >= cutoff:
            violations.append({
                "run_name": str(run_name),
                "train_label_max_ts": prior_labels.max().isoformat(),
                "current_decision_min_ts": cutoff.isoformat(),
            })
    return {
        "fold_count": max(0, len(runs) - 4),
        "violations": violations,
        "violation_count": len(violations),
    }


def _label_visibility_audit(features: pd.DataFrame, decision: pd.Series) -> dict[str, Any]:
    """Check that the outcome label is not available at or before entry."""
    close = _label_timestamp(features)
    invalid = close.isna() | close.le(decision)
    samples = features.loc[invalid, ["market_id", "run_name", "slug", "first_quote_ts"]].head(5)
    return {
        "rows_checked": int(len(features)),
        "violation_count": int(invalid.sum()),
        "violation_samples": [
            {key: str(value) for key, value in row.items()}
            for row in samples.to_dict("records")
        ],
    }


def audit(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    features = features.sort_values("first_quote_ts", kind="mergesort").reset_index(drop=True)
    decision = pd.to_datetime(features["first_quote_ts"], utc=True) + pd.to_timedelta(
        DECISION_OFFSET_SECONDS, unit="s"
    )
    feature_checks: list[dict[str, Any]] = []
    all_violations: list[dict[str, Any]] = []
    for name in frozen.FEATURES:
        cutoff = FEATURE_CUTOFF_SECONDS.get(name)
        if cutoff is None:
            raise RuntimeError(f"UNMAPPED_FROZEN_FEATURE:{name}")
        observed_ts = _feature_timestamp(features, name)
        violation_mask = observed_ts > decision
        count = int(violation_mask.fillna(True).sum())
        item = {
            "feature": name,
            "declared_cutoff_seconds": float(cutoff),
            "rows_checked": int(len(features)),
            "rows_with_timestamp_violation": count,
            "max_observed_after_decision_seconds": (
                float((observed_ts[violation_mask] - decision[violation_mask]).dt.total_seconds().max())
                if bool(violation_mask.any()) else 0.0
            ),
        }
        feature_checks.append(item)
        if count:
            sample = features.loc[violation_mask, ["market_id", "run_name", "slug", "first_quote_ts"]].head(5)
            for row in sample.to_dict("records"):
                all_violations.append({"feature": name, **{key: str(value) for key, value in row.items()}})

    label_audit = _run_boundary_audit(features, runs)
    label_visibility = _label_visibility_audit(features, decision)
    temporal_pass = (
        not all_violations
        and label_audit["violation_count"] == 0
        and label_visibility["violation_count"] == 0
    )
    result: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "feature_rows": int(len(features)),
        "complete_runs": int(len(runs)),
        "coverage_rows": int(len(coverage)),
        "frozen_feature_columns": list(frozen.FEATURES),
        "decision_contract": {
            "entry_seconds_after_start": DECISION_OFFSET_SECONDS,
            "required_feature_end_le_decision": True,
            "labels_must_be_after_decision": True,
            "training_runs_strictly_before_scored_run": True,
        },
        "feature_checks": feature_checks,
        "feature_violation_count": int(sum(item["rows_with_timestamp_violation"] for item in feature_checks)),
        "violation_samples": all_violations,
        "label_boundary_audit": label_audit,
        "label_visibility_audit": label_visibility,
        "temporal_contract_passed": bool(temporal_pass),
        "paper_frontier_status": "causal_contract_passed" if temporal_pass else "causal_contract_failed_quarantine",
        "input_identity_sha256": _identity_hash(features),
        "model_refit": False,
        "new_data_collected": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "paper_only": True,
        "candidate_row_identity_note": "The saved public H534 summary is aggregate-only; this audit reports the frozen feature contract and does not invent per-row authenticated identity.",
        "decision": "invalidate_affected_paper_rows_and_register_separate_causal_repair" if not temporal_pass else "retain_temporal_contract_for_next_audit",
    }
    output.mkdir(parents=True, exist_ok=True)
    path = output / "report.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["report_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "format": result["format"],
        "feature_rows": result["feature_rows"],
        "feature_violation_count": result["feature_violation_count"],
        "label_boundary_violation_count": result["label_boundary_audit"]["violation_count"],
        "label_visibility_violation_count": result["label_visibility_audit"]["violation_count"],
        "temporal_contract_passed": result["temporal_contract_passed"],
        "input_identity_sha256": result["input_identity_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
