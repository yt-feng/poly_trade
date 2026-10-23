"""Private, paired retrospective audit using one fitted prediction stream.

This runner records source membership and scores for both policy arms. It
neither authenticates executions nor enables orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_policy_audit as base


ARMS = ("legacy_control", "cost_checked")
RESULT_FIELDS = ("scope", "block_index", "slippage", "latency_seconds", "depth_haircut", "runs",
    "fills", "ending_cash", "first_ten_fills", "first_ten_ending_cash", "first_ten_passed",
    "failure_reason", "locked_shares", "ending_cash_available_ts")
CHAIN_FIELDS = ("market_id", "run_name", "slug", "entry_ts", "cash_before_entry", "cash_after_entry",
    "shares", "entry_fill_price", "entry_cost", "entry_fee", "intended_arrival_ts", "entry_quote_gap_seconds",
    "effective_entry_latency_seconds", "fraction", "depth_capacity_ratio", "status", "exit_ts",
    "exit_fill_price", "payout", "cash_release_ts", "terminal_proxy_won")
ATTEMPT_FIELDS = ("market_id", "status")
FINGERPRINT_VERSION = "legacy_result_allowlist_round9_v1"


def _normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.number)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite_reference_result")
        number = round(number, 9)
        return int(number) if number.is_integer() else number
    if value is None or isinstance(value, str):
        return value
    raise ValueError("unsupported_reference_value")


def normalized_results(report_or_rows: Any) -> list[dict[str, Any]]:
    rows = report_or_rows["results"] if isinstance(report_or_rows, dict) else report_or_rows
    result = []
    for row in rows:
        item = {key: row[key] for key in RESULT_FIELDS if key in row}
        item["chain"] = [{key: event[key] for key in CHAIN_FIELDS if key in event} for event in row.get("chain", [])]
        item["attempts"] = [{key: event[key] for key in ATTEMPT_FIELDS if key in event} for event in row.get("attempts", [])]
        result.append(_normalized(item))
    return sorted(result, key=lambda row: (row["scope"], row["block_index"], row["slippage"], row["latency_seconds"], row["depth_haircut"]))


def results_fingerprint(report_or_rows: Any) -> str:
    canonical = json.dumps(normalized_results(report_or_rows), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _candidate_counts(counts: dict[str, Any]) -> dict[str, int]:
    result = {}
    for key, value in counts.items():
        if type(value) is not int or value < 0:
            raise ValueError("invalid_reference_candidate_count")
        normalized_key = str(float(key))
        if normalized_key in result:
            raise ValueError("duplicate_reference_slippage")
        result[normalized_key] = value
    return result


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    base.validate_config(config)
    if config.get("evaluation_mode") != "paired_cost_gate":
        raise ValueError("paired_cost_mode_required")
    if not re.fullmatch(r"[a-f0-9]{64}", str(config.get("control_reference_results_sha256", ""))):
        raise ValueError("reference_fingerprint_required")
    if not isinstance(config.get("control_reference_candidate_counts"), dict):
        raise ValueError("reference_candidate_counts_required")
    counts = _candidate_counts(config["control_reference_candidate_counts"])
    if set(counts) != {str(float(value)) for value in config["slippages"]}:
        raise ValueError("reference_slippage_mismatch")
    if len(set(map(float, config["slippages"]))) != len(config["slippages"]):
        raise ValueError("duplicate_slippage")
    for field in ("max_scored_runs", "max_feature_rows", "max_replay_cells"):
        if field in config and (type(config[field]) is not int or config[field] < 1):
            raise ValueError("invalid_computation_budget")
    return config


class FrozenScores:
    """Predictor-shaped access to an already computed, indexed vector."""

    def __init__(self, probabilities: pd.Series):
        if not probabilities.index.is_unique:
            raise ValueError("duplicate_score_index")
        values = probabilities.to_numpy(dtype=float)
        if not np.isfinite(values).all() or not ((values >= 0) & (values <= 1)).all():
            raise ValueError("invalid_frozen_probabilities")
        self.probabilities = probabilities.copy()

    def predict_proba(self, values: pd.DataFrame) -> np.ndarray:
        if not values.index.equals(self.probabilities.index):
            raise ValueError("frozen_score_row_alignment_mismatch")
        up = self.probabilities.to_numpy(dtype=float)
        return np.column_stack([1.0 - up, up])


def score_once(model: Any, train: pd.DataFrame, current: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    model.fit(train[features], train["outcome_up"].astype(int))
    probability = np.asarray(model.predict_proba(current[features]))
    if probability.shape != (len(current), 2):
        raise ValueError("invalid_prediction_shape")
    scored = current.copy()
    scored["q_up"] = probability[:, 1]
    FrozenScores(scored["q_up"])
    return scored


def candidate_arms(scored: pd.DataFrame, config: dict[str, Any]) -> dict[str, dict[float, pd.DataFrame]]:
    frozen = FrozenScores(scored["q_up"])
    arms: dict[str, dict[float, pd.DataFrame]] = {name: {} for name in ARMS}
    for slippage in map(float, config["slippages"]):
        for name, candidate_function in (("legacy_control", base.signal._candidate_frame), ("cost_checked", base.cost_checked_candidate_frame)):
            frame = candidate_function(frozen, pd.Series(dtype=float), scored, slippage)
            if not frame.empty:
                frame = frame[frame["q_side"].ge(float(config["confidence_floor"])) & frame["price"].ge(float(config["favorite_price_floor"]))].copy()
            arms[name][slippage] = frame
    return arms


def compare_control(results: list[dict[str, Any]], counts: dict[str, int], config: dict[str, Any]) -> dict[str, Any]:
    actual_fingerprint = results_fingerprint(results)
    fingerprint_matches = actual_fingerprint == config["control_reference_results_sha256"]
    counts_match = _candidate_counts(counts) == _candidate_counts(config["control_reference_candidate_counts"])
    return {"fingerprint_version": FINGERPRINT_VERSION, "actual_results_sha256": actual_fingerprint,
        "expected_results_sha256": config["control_reference_results_sha256"],
        "results_match": fingerprint_matches, "candidate_counts_match": counts_match,
        "expected_candidate_counts": config["control_reference_candidate_counts"], "actual_candidate_counts": counts,
        "control_reproduced": bool(fingerprint_matches and counts_match)}


def _private_json(path: Path, payload: Any) -> None:
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(payload, indent=2, default=str, allow_nan=False) + "\n")


def _private_csv(path: Path, frame: pd.DataFrame, append: bool = False) -> None:
    exists = path.exists()
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    frame.to_csv(path, index=False, mode="a" if append else "w", header=not append or not exists)


def replay_arm(candidates: dict[float, pd.DataFrame], test_runs: list[str], blocks: list[list[str]],
               quotes: dict[str, pd.DataFrame], config: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for slippage, frame in candidates.items():
        for latency in config["latencies_seconds"]:
            for haircut in config["depth_haircuts"]:
                for index, block in enumerate([test_runs] + blocks):
                    result = base.replay_policy(frame, block, quotes, latency_seconds=float(latency),
                        depth_haircut=float(haircut), thresholds=tuple(config["depth_thresholds"]),
                        cash_release_delay_after_close_seconds=float(config["cash_release_delay_after_close_seconds"]))
                    results.append({"scope": "pooled_single_ledger" if index == 0 else "disjoint_diagnostic_block",
                        "block_index": index, "runs": block, "slippage": slippage, "latency_seconds": latency,
                        "depth_haircut": haircut, **result})
    return results


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    config = validate_config(config)
    source_commit = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    if source_commit != config["source_commit"]:
        raise ValueError("source_commit_mismatch")
    # Refuse to append provenance to a previous computation's files.
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    features, runs, coverage, quotes = base.prepare_archive(source_root, config)
    warmup = int(config["minimum_training_runs"])
    test_runs = runs[warmup:]
    blocks = base.disjoint_blocks(test_runs, int(config["evaluation_block_runs"]))
    if len(features) > int(config.get("max_feature_rows", 1_000_000)) or len(test_runs) > int(config.get("max_scored_runs", 500)):
        raise ValueError("declared_computation_budget_exceeded")
    replay_cells = 2 * len(config["slippages"]) * len(config["latencies_seconds"]) * len(config["depth_haircuts"]) * (len(blocks) + 1)
    if replay_cells > int(config.get("max_replay_cells", 10000)):
        raise ValueError("declared_replay_budget_exceeded")
    pending_frames: dict[str, dict[float, list[pd.DataFrame]]] = {
        arm: {float(value): [] for value in config["slippages"]} for arm in ARMS}
    fit_records = []
    with base.isolated_signal_contract(config["features"], float(config["decision_cutoff_seconds"]) + float(config["entry_delay_seconds"])):
        for index, name in enumerate(runs):
            if index < warmup:
                continue
            current = features[features["run_name"].astype(str).eq(name)].copy()
            train = base.causal_training_rows(features, runs[:index], current)
            member_ids = sorted(train["market_id"].astype(str))
            member_hash = hashlib.sha256(json.dumps(member_ids, separators=(",", ":")).encode()).hexdigest()
            cutoff = current["decision_ts"].min().isoformat()
            record = {"scored_run": name, "training_rows": len(train), "scored_rows": len(current),
                "decision_min_ts": cutoff, "training_label_max_ts": train["label_observed_ts"].max().isoformat() if not train.empty else None,
                "training_market_ids_sha256": member_hash, "training_unique_market_count": len(set(member_ids)),
                "actual_model_fit_calls": 0, "actual_model_predict_calls": 0}
            members = train[["market_id", "label_observed_ts"]].copy()
            members.insert(0, "fit_run", name)
            members["decision_cutoff_ts"] = cutoff
            _private_csv(output / "training-members.csv", members, append=True)
            if len(train) < 100 or train["outcome_up"].nunique() < 2:
                record["status"] = "insufficient_causal_training_data"
                unscored = current.copy(); unscored["q_up"] = np.nan
                unscored["fit_run"] = name; unscored["score_status"] = record["status"]
                _private_csv(output / "scores.csv", unscored, append=True)
                fit_records.append(record)
                continue
            model = ExtraTreesClassifier(**config["model_params"])
            scored = score_once(model, train, current, config["features"])
            scored["fit_run"] = name; scored["score_status"] = "fit_with_prior_available_labels"
            _private_csv(output / "scores.csv", scored, append=True)
            arms = candidate_arms(scored, config)
            for arm in ARMS:
                for slippage, frame in arms[arm].items():
                    if not frame.empty:
                        pending_frames[arm][slippage].append(frame)
            record.update(status="fit_with_prior_available_labels", actual_model_fit_calls=1, actual_model_predict_calls=1)
            fit_records.append(record)
    fitted = sum(record["actual_model_fit_calls"] for record in fit_records)
    status, strategy_evaluated = base.evaluation_status(len(features), fitted)
    arm_reports = {}
    for arm in ARMS:
        candidates = {slippage: pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                      for slippage, frames in pending_frames[arm].items()}
        all_frames = [frame for frame in candidates.values() if not frame.empty]
        combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame(columns=["market_id", "q_up", "slippage"])
        _private_csv(output / f"{arm}-candidates.csv", combined)
        arm_reports[arm] = {"candidate_rows_by_slippage": {str(s): len(frame) for s, frame in candidates.items()},
            "selection_edge_basis": "legacy_quoted_ask_plus_fee" if arm == "legacy_control" else "fill_price_including_slippage_plus_fee",
            "results": replay_arm(candidates, test_runs, blocks, quotes, config) if strategy_evaluated else []}
    for filename, columns in (("scores.csv", ["market_id", "q_up", "fit_run", "score_status"]),
                              ("training-members.csv", ["fit_run", "market_id", "label_observed_ts", "decision_cutoff_ts"])):
        if not (output / filename).exists():
            _private_csv(output / filename, pd.DataFrame(columns=columns))
    control = compare_control(arm_reports["legacy_control"]["results"], arm_reports["legacy_control"]["candidate_rows_by_slippage"], config)
    reproduced = strategy_evaluated and control["control_reproduced"]
    if strategy_evaluated and not reproduced:
        status = "evaluated_control_mismatch"
    code_commit = subprocess.check_output(["git", "-C", str(CURRENT_DIR), "rev-parse", "HEAD"], text=True).strip()
    manifest = {path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                for path in sorted(output.iterdir()) if path.is_file()}
    report = {"format": "offline-paired-cost-audit-v1", "protocol": config,
        "protocol_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "source_commit": source_commit, "code_commit": code_commit, "coverage": coverage, "run_order": runs,
        "evaluation_status": status, "strategy_evaluated": strategy_evaluated,
        "fitted_scored_runs": fitted, "model_scored_rows": sum(r["scored_rows"] for r in fit_records if r["actual_model_fit_calls"]),
        "actual_model_fit_calls": fitted, "actual_model_predict_calls": sum(r["actual_model_predict_calls"] for r in fit_records),
        "fit_records": fit_records, "evaluation_blocks": blocks, "arms": arm_reports,
        "control_verification": control, "control_reproduced": bool(reproduced), "causal_comparison_valid": bool(reproduced),
        "comparison_status": "paired_retrospective_control_reproduced" if reproduced else "not_admitted_control_not_reproduced",
        "paired_contract": {"same_fitted_model": True, "same_predictions": True, "one_predict_per_scored_run": True,
            "only_arm_difference": "entry_selection_net_edge_cost_basis", "fingerprint_version": FINGERPRINT_VERSION,
            "candidate_csv_legacy_clock_fields": {
                "settlement_ts": "legacy_signal_market_start_clock_retained_for_reproduction_not_used_by_replay",
                "cash_release_ts": "legacy_signal_release_clock_retained_for_reproduction_not_used_by_replay"},
            "execution_clock_fields": ["market_close_ts", "label_observed_ts", "chain.cash_release_ts"]},
        "artifacts": manifest, "evaluation_class": "retrospective_paired_correction_not_untouched_holdout",
        "actual_fills": 0, "verified_canary_fills": 0, "live_orders_enabled": False, "new_data_collected": False}
    _private_json(output / "summary.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, json.loads(args.config.read_text()), args.output)


if __name__ == "__main__":
    main()
