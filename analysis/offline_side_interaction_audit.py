"""Paired private audit of a fixed side-conditioned linear residual class.

The immutable parent feature, membership and execution contracts must reproduce
before any new model is fitted. Parent predictions remain a cached reference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_bid_residual_audit as base

FEATURES = base.FEATURES
FINGERPRINT_VERSION = "timed_quote_full_round10_v1"
CANDIDATE_FIELDS = ["market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts",
                    "decision_ask_price", "decision_sell_size", "predicted_exit_bid_price"]
UNCHANGED_FILES = {"analysis/offline_bid_residual_audit.py", "analysis/timed_quote_execution.py",
    "analysis/offline_entry_limit_audit.py", "analysis/offline_paired_cost_audit.py", "analysis/offline_policy_audit.py"}
TREATMENT = {"side_sign": {"up": 1, "down": -1},
    "training_target": "side_sign * physical_bid_residual_target", "raw_model_output": "oriented_signed_residual",
    "physical_residual_prediction": "side_sign * raw_model_output",
    "physical_future_bid_prediction": "clip(decision_bid_price + physical_residual_prediction,0,1)",
    "intercept": "fit_and_invert_with_same_side_sign", "ridge_alpha": 1, "ridge_solver": "svd",
    "scaler": "same_original_features_train_only_standard_scaler"}
MEMBER_FIELDS = ["fit_run", "market_id", "label_available_ts", "bid_residual_target", "decision_cutoff_ts"]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def normalized_result(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite_control_result")
        return round(value, 10)
    if isinstance(value, dict):
        return {key: normalized_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized_result(item) for item in value]
    return value


def result_fingerprint(cell: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(normalized_result(cell))).hexdigest()


def ids_sha256(ids: Any) -> str:
    return hashlib.sha256(canonical(sorted(str(value) for value in ids))).hexdigest()


def cohort_contract(frame: pd.DataFrame) -> dict[str, Any]:
    known = frame["future_bid_price"].notna()
    return {"scored_rows": len(frame), "known_targets": int(known.sum()), "unknown_targets": int((~known).sum()),
        "scored_ids_sha256": ids_sha256(frame["market_id"]),
        "known_ids_sha256": ids_sha256(frame.loc[known, "market_id"]),
        "unknown_ids_sha256": ids_sha256(frame.loc[~known, "market_id"])}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("evaluation_mode") != "paired_side_interaction":
        raise ValueError("side_interaction_mode_required")
    parent = config.get("base_protocol")
    if not isinstance(parent, dict):
        raise ValueError("immutable_base_protocol_required")
    base.validate_config(parent)
    for field in ("expected_code_commit", "source_commit", "parent_code_commit"):
        if not re.fullmatch(r"[a-f0-9]{40}", str(config.get(field, ""))):
            raise ValueError("invalid_commit_binding")
    bindings = config.get("parent_bindings", {})
    if (config["source_commit"] != parent["source_commit"]
            or config["parent_code_commit"] != parent["expected_code_commit"]
            or hashlib.sha256(canonical(parent)).hexdigest() != bindings.get("protocol_sha256")):
        raise ValueError("parent_protocol_binding_mismatch")
    required_hashes = ("summary_sha256", "protocol_sha256", "features_csv_sha256", "scores_csv_sha256",
        "training_members_csv_sha256", "model_parameters_sha256", "candidates_csv_sha256")
    for field in required_hashes:
        if not re.fullmatch(r"[a-f0-9]{64}", str(bindings.get(field, ""))):
            raise ValueError("missing_parent_artifact_binding")
    unchanged = bindings.get("unchanged_code_file_sha256", {})
    if set(unchanged) != UNCHANGED_FILES or any(not re.fullmatch(r"[a-f0-9]{64}", str(value)) for value in unchanged.values()):
        raise ValueError("immutable_helper_bindings_required")
    if config.get("treatment") != TREATMENT or config.get("control_model_refit_allowed") is not False:
        raise ValueError("fixed_target_orientation_contract_required")
    if config.get("live_orders_enabled") is not False or config.get("actual_fills") != 0:
        raise ValueError("offline_only_contract_required")
    if config.get("control_result_fingerprint_version") != FINGERPRINT_VERSION:
        raise ValueError("control_fingerprint_version_mismatch")
    for field in ("max_new_model_fit_calls", "max_new_model_predict_calls", "max_replay_cells", "max_private_config_bytes",
                  "expected_control_candidate_count", "expected_control_result_count"):
        if type(config.get(field)) is not int or config[field] < 1:
            raise ValueError("invalid_budget_or_reference_count")
    if config["max_private_config_bytes"] > 262144 or len(canonical(config)) > config["max_private_config_bytes"]:
        raise ValueError("private_input_budget_exceeded")
    runs = parent["run_order"][parent["minimum_training_runs"]:]
    cells = 1 + len(base.disjoint_blocks(runs, parent["evaluation_block_runs"]))
    if (config["expected_control_result_count"] != cells or 2 * cells > config["max_replay_cells"]
            or len(runs) > config["max_new_model_fit_calls"] or len(runs) > config["max_new_model_predict_calls"]):
        raise ValueError("computation_budget_or_scope_mismatch")
    expected = config.get("expected_control_result_fingerprints")
    if not isinstance(expected, list) or len(expected) != cells or any(not re.fullmatch(r"[a-f0-9]{64}", str(v)) for v in expected):
        raise ValueError("complete_control_fingerprints_required")
    records = config.get("expected_parent_fit_records")
    if not isinstance(records, list) or [r.get("run_name") for r in records] != runs:
        raise ValueError("complete_parent_fit_records_required")
    expected_scored = {r["run_name"] for r in records if r.get("status") == "fit_with_prior_available_labels"}
    if set(config.get("expected_scored_cohort_per_run", {})) != expected_scored:
        raise ValueError("complete_scored_cohort_bindings_required")
    for item in [config.get("expected_scored_cohort", {})] + list(config["expected_scored_cohort_per_run"].values()):
        for field in ("scored_ids_sha256", "known_ids_sha256", "unknown_ids_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", str(item.get(field, ""))):
                raise ValueError("invalid_cohort_hash")
        if any(type(item.get(k)) is not int or item[k] < 0 for k in ("scored_rows", "known_targets", "unknown_targets")):
            raise ValueError("invalid_cohort_count")
        if item["scored_rows"] != item["known_targets"] + item["unknown_targets"]:
            raise ValueError("invalid_cohort_partition")
    if config.get("cached_parent_prediction_reference_kind") != "audited_cached_score_metrics_not_refitted_or_fully_rescored_control":
        raise ValueError("cached_reference_boundary_required")
    frozen_candidates(config)
    return config


def frozen_candidates(config: dict[str, Any]) -> pd.DataFrame:
    rows = config.get("frozen_control_candidates")
    if (config.get("frozen_control_candidate_fields") != CANDIDATE_FIELDS or not isinstance(rows, list)
            or len(rows) != config["expected_control_candidate_count"]
            or any(set(row) != set(CANDIDATE_FIELDS) or any(not isinstance(v, str) for v in row.values()) for row in rows)):
        raise ValueError("frozen_control_projection_contract_mismatch")
    if hashlib.sha256(canonical(rows)).hexdigest() != config.get("frozen_control_candidate_rows_sha256"):
        raise ValueError("frozen_control_projection_hash_mismatch")
    frame = pd.DataFrame(rows, columns=CANDIDATE_FIELDS)
    for column in ("decision_ask_price", "decision_sell_size", "predicted_exit_bid_price"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
        if not np.isfinite(frame[column]).all():
            raise ValueError("nonfinite_frozen_candidate")
    for column in ("decision_ts", "market_close_ts"):
        for value in frame[column]:
            if pd.Timestamp(value).tzinfo is None:
                raise ValueError("unqualified_frozen_candidate_clock")
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="raise")
    if (frame["market_id"].duplicated().any() or frame["slug"].duplicated().any()
            or not frame["side"].isin(["up", "down"]).all()
            or not frame["market_id"].eq(frame["run_name"] + "::" + frame["slug"]).all()
            or not frame["decision_ts"].lt(frame["market_close_ts"]).all()
            or not frame["decision_ask_price"].between(0, 1, inclusive="neither").all()
            or not frame["predicted_exit_bid_price"].between(0, 1).all()
            or not frame["decision_sell_size"].ge(0).all()):
        raise ValueError("invalid_frozen_candidate_identity_or_domain")
    return frame


def verified_inputs(source_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    source, code = base.integrity.verified_commits(source_root, config)
    observed = {name: file_sha256(CURRENT_DIR.parent / name)
                for name in config["parent_bindings"]["unchanged_code_file_sha256"]}
    if observed != config["parent_bindings"]["unchanged_code_file_sha256"]:
        raise ValueError("immutable_parent_helper_bytes_changed")
    return {"source_commit": source, "code_commit": code, "unchanged_code_file_sha256": observed}


def side_sign(frame: pd.DataFrame) -> np.ndarray:
    if "side" not in frame or not frame["side"].isin(["up", "down"]).all():
        raise ValueError("invalid_decision_side")
    return frame["side"].map({"up": 1., "down": -1.}).to_numpy(dtype=float)


def fit_predict_once(train: pd.DataFrame, current: pd.DataFrame,
                     base_config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    if base_config.get("features") != FEATURES or base_config.get("ridge_alpha") != 1 or base_config.get("ridge_solver") != "svd":
        raise ValueError("fixed_linear_model_contract_required")
    train_sign, test_sign = side_sign(train), side_sign(current)
    model = make_pipeline(StandardScaler(), Ridge(alpha=float(base_config["ridge_alpha"]), solver=base_config["ridge_solver"]))
    fit_calls = predict_calls = 0
    try:
        fit_calls = 1
        model.fit(train[FEATURES], train_sign * train["bid_residual_target"].to_numpy(dtype=float))
        predict_calls = 1
        oriented = np.asarray(model.predict(current[FEATURES]), dtype=float)
        if not np.isfinite(oriented).all():
            raise ValueError("nonfinite_oriented_prediction")
        scored = current.copy()
        scored["predicted_oriented_residual_raw"] = oriented
        scored["predicted_bid_residual_raw"] = test_sign * oriented
        scored["predicted_exit_bid_unclipped"] = scored["decision_bid_price"] + scored["predicted_bid_residual_raw"]
        scored["predicted_exit_bid_price"] = scored["predicted_exit_bid_unclipped"].clip(0, 1)
        scaler, ridge = model.steps[0][1], model.steps[1][1]
        return scored, {"scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
            "ridge_coef": ridge.coef_.tolist(), "ridge_intercept": float(ridge.intercept_),
            "orientation": {"side_sign": {"up": 1, "down": -1},
                "training_target": "side_sign * physical_bid_residual_target",
                "prediction": "physical_residual = side_sign * oriented_model_output", "intercept_inverted_with_side": True}}
    except Exception as exc:
        exc.audit_fit_calls = fit_calls
        exc.audit_predict_calls = predict_calls
        raise


def build_fit_plan(frame: pd.DataFrame, parent: dict[str, Any]) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    plans = []
    scored_parts = []
    runs = parent["run_order"]
    for index, run in enumerate(runs):
        if index < parent["minimum_training_runs"]:
            continue
        current = frame[frame["run_name"].eq(run)].copy()
        train = base.causal_training_rows(frame, runs[:index], current)
        cutoff = current["decision_ts"].min() if not current.empty else None
        known_runs = int(train["run_name"].nunique())
        eligible = not current.empty and len(train) >= parent["minimum_training_labels"] and known_runs >= parent["minimum_training_runs"]
        status = ("fit_with_prior_available_labels" if eligible else "empty_evaluation_cohort" if current.empty else
                  "insufficient_prior_labeled_runs" if known_runs < parent["minimum_training_runs"] else "insufficient_prior_known_quote_targets")
        reference = {"run_name": run, "training_rows": len(train), "training_known_runs": known_runs,
            "scored_rows": len(current) if eligible else 0, "evaluation_cohort_rows": len(current),
            "decision_cutoff_ts": cutoff.isoformat() if cutoff is not None else None,
            "training_label_max_ts": train["label_available_ts"].max().isoformat() if not train.empty else None,
            "training_market_ids_sha256": ids_sha256(train["market_id"]),
            "actual_model_fit_calls": int(eligible), "actual_model_predict_calls": int(eligible), "status": status}
        plans.append({"reference": reference, "train_index": train.index.to_numpy(), "test_index": current.index.to_numpy(), "eligible": eligible})
        if eligible:
            scored_parts.append(current)
    cohort = pd.concat(scored_parts, ignore_index=True) if scored_parts else frame.iloc[:0].copy()
    return plans, cohort


def replay_arm(candidates: pd.DataFrame, quotes: dict[str, pd.DataFrame], parent: dict[str, Any]) -> list[dict[str, Any]]:
    tests = parent["run_order"][parent["minimum_training_runs"]:]
    blocks = base.disjoint_blocks(tests, parent["evaluation_block_runs"])
    results = []
    for index, runs in enumerate([tests] + blocks):
        result = base.execution.replay_timed_quotes(candidates, runs, quotes,
            hold_seconds=parent["hold_seconds"], entry_delay_seconds=parent["entry_delay_seconds"],
            latency_seconds=parent["latency_seconds"], max_quote_delay_seconds=parent["max_quote_delay_seconds"],
            edge_floor=parent["edge_floor"], fee_rate=parent["fee_rate"], slippage=parent["slippage"],
            depth_haircut=parent["depth_haircut"], thresholds=tuple(parent["depth_thresholds"]),
            cash_release_delay_after_close_seconds=parent["cash_release_delay_after_close_seconds"],
            initial_cash=parent["initial_cash"], minimum_order_usd=parent["minimum_order_usd"],
            minimum_order_shares=parent["minimum_order_shares"], required_fills=parent["required_fills"])
        results.append({"scope": "pooled_single_ledger" if index == 0 else "disjoint_diagnostic_block", "block_index": index,
            "runs": runs, "slippage": parent["slippage"], "latency_seconds": parent["latency_seconds"],
            "depth_haircut": parent["depth_haircut"], **result})
    return results


def gate(report: dict[str, Any], name: str, actual: Any, expected: Any) -> None:
    passed = actual == expected
    report["pre_fit_gates"].append({"gate": name, "passed": passed, "actual": actual, "expected": expected})
    if not passed:
        raise ValueError(name + "_mismatch")


def physical_persistence_reference(cohort: pd.DataFrame) -> dict[str, Any]:
    values = cohort.copy()
    values["predicted_exit_bid_price"] = values["decision_bid_price"]
    metrics = base.prediction_metrics(values)
    def subset(item):
        return {key: item[key] for key in ("known_targets", "baseline_squared_error_sum", "baseline_mse")}
    return {"aggregate_known_targets": subset(metrics["aggregate_known_targets"]),
            "scored_rows": metrics["scored_rows"], "unknown_targets_retained": metrics["unknown_targets_retained"],
            "per_run": [{"run_name": row["run_name"], **subset(row)} for row in metrics["per_run"]]}


def persistence_subset(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = ("known_targets", "baseline_squared_error_sum", "baseline_mse")
    return {"aggregate_known_targets": {k: metrics["aggregate_known_targets"][k] for k in keys},
        "scored_rows": metrics["scored_rows"], "unknown_targets_retained": metrics["unknown_targets_retained"],
        "per_run": [{"run_name": row["run_name"], **{k: row[k] for k in keys}} for row in metrics["per_run"]]}


def predictive_comparison(metrics: dict[str, Any], cached: dict[str, Any], scored: pd.DataFrame) -> dict[str, Any]:
    def compare(current: dict[str, Any], prior: dict[str, Any]) -> dict[str, Any]:
        model, control, persistence = (current["model_squared_error_sum"], prior["model_squared_error_sum"],
                                        current["baseline_squared_error_sum"])
        rows = current["known_targets"]
        result = {"known_targets": rows, "treatment_sse": model, "cached_control_sse": control,
                  "persistence_sse": persistence,
                  "treatment_mse": model / rows if model is not None and rows else None,
                  "cached_control_mse": control / rows if control is not None and rows else None,
                  "persistence_mse": persistence / rows if persistence is not None and rows else None}
        for name, reference in (("cached_control", control), ("persistence", persistence)):
            delta = model - reference if model is not None and reference is not None else None
            result[name + "_sse_delta"] = delta
            result[name + "_mse_delta"] = delta / rows if delta is not None and rows else None
            result[name + "_relative_error_delta_percent"] = 100 * delta / reference if delta is not None and reference else None
        return result
    aggregate = compare(metrics["aggregate_known_targets"], cached["aggregate_known_targets"])
    prior_runs = {row["run_name"]: row for row in cached["per_run"]}
    per_run = [{"run_name": row["run_name"], **compare(row, prior_runs[row["run_name"]])} for row in metrics["per_run"]]
    side_diagnostics = {}
    for side, part in scored.groupby("side", sort=True):
        side_metrics = base.prediction_metrics(part)
        side_diagnostics[str(side)] = {"scored_rows": len(part), "unknown_targets_retained": side_metrics["unknown_targets_retained"],
            "treatment_and_persistence": side_metrics["aggregate_known_targets"], "cached_control_by_side": None,
            "control_unavailable_reason": "Cached reference does not include per-side predictions; no control refit or rescore performed."}
    error = aggregate["treatment_sse"]
    return {"aggregate": aggregate, "paired_per_run": per_run, "side_diagnostics": side_diagnostics,
        "treatment_beats_both_with_declared_margin": bool(error is not None
            and error + 1e-10 < aggregate["cached_control_sse"] and error + 1e-10 < aggregate["persistence_sse"]),
        "absolute_sse_margin": 1e-10, "basis": "same_physical_known_targets_cached_audited_control_not_independent_trials"}


def turnover_summary(results: list[dict[str, Any]], initial_cash: float) -> list[dict[str, Any]]:
    summaries = []
    for row in results:
        chain = row["chain"]
        fully_resolved = bool(chain) and row["locked_shares"] == 0 and row["resolved_fills"] == len(chain)
        elapsed = ((pd.Timestamp(row["ending_cash_available_ts"]) - pd.Timestamp(chain[0]["entry_ts"])).total_seconds()
                   if fully_resolved else None)
        log_growth = math.log(row["ending_cash"] / initial_cash) if fully_resolved and row["ending_cash"] > 0 else None
        summaries.append({"scope": row["scope"], "block_index": row["block_index"],
            "fully_resolved": fully_resolved, "ending_liquid_cash": row["ending_cash"], "locked_shares": row["locked_shares"],
            "first_entry_ts": chain[0]["entry_ts"] if chain else None,
            "assumed_cash_available_ts": row["ending_cash_available_ts"],
            "fully_resolved_elapsed_seconds": elapsed, "fully_resolved_net_log_growth": log_growth,
            "fully_resolved_net_log_growth_per_hour": log_growth * 3600 / elapsed if log_growth is not None and elapsed and elapsed > 0 else None,
            "resolved_cash_cycle_seconds": [(pd.Timestamp(e["cash_release_ts"]) - pd.Timestamp(e["entry_ts"])).total_seconds()
                                             for e in chain if e["status"] == "timed_quote_exit"],
            "basis": "declared_cash_release_assumption_not_observed_account_receipt"})
    return summaries


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    report = {"format": "offline-side-interaction-audit-v1", "evaluation_status": "comparison_invalid",
        "strategy_evaluated": False, "comparison_valid": False, "control_execution_reproduced": False,
        "actual_model_fit_calls": 0, "actual_model_predict_calls": 0, "model_scored_rows": 0, "fitted_scored_runs": 0,
        "control_model_fit_calls": 0, "control_model_predict_calls": 0, "control_full_predictions_reproduced": False,
        "fit_records": [], "pre_fit_gates": [], "arms": {}, "replayed_cells": 0,
        "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False}
    stage = "configuration"
    try:
        validate_config(config)
        parent = config["base_protocol"]
        report.update(protocol=config, protocol_sha256=hashlib.sha256(canonical(config)).hexdigest(),
            cache_reference_kind=config["cached_parent_prediction_reference_kind"], evaluation_class=config["evaluation_class"])
        stage = "source_and_code_bindings"
        report.update(verified_inputs(source_root, config))
        report["pre_fit_gates"].append({"gate": stage, "passed": True})
        stage = "feature_reconstruction"
        frame, quotes, coverage = base.prepare_archive(source_root, parent)
        report["coverage"] = coverage
        base.storage._private_csv(output / "features.csv", frame)
        gate(report, "parent_features_csv_sha256", file_sha256(output / "features.csv"), config["parent_bindings"]["features_csv_sha256"])
        stage = "membership_and_cohort_reconstruction"
        plans, cohort = build_fit_plan(frame, parent)
        references = [plan["reference"] for plan in plans]
        gate(report, "parent_fit_records", references, config["expected_parent_fit_records"])
        gate(report, "scored_cohort", cohort_contract(cohort), config["expected_scored_cohort"])
        per_run = {str(run): cohort_contract(part) for run, part in cohort.groupby("run_name", sort=False)}
        gate(report, "scored_cohort_per_run", per_run, config["expected_scored_cohort_per_run"])
        if cohort.empty:
            raise ValueError("empty_bound_scored_cohort")
        report["reconstructed_parent_fit_records"] = references
        member_path = output / "training-members.csv"
        for plan in plans:
            if not plan["eligible"]:
                continue
            reference = plan["reference"]
            member = frame.loc[plan["train_index"], ["market_id", "label_available_ts", "bid_residual_target"]].copy()
            member.insert(0, "fit_run", reference["run_name"])
            member["decision_cutoff_ts"] = pd.Timestamp(reference["decision_cutoff_ts"])
            base.storage._private_csv(member_path, member[MEMBER_FIELDS], append=True)
        if not member_path.exists():
            base.storage._private_csv(member_path, pd.DataFrame(columns=MEMBER_FIELDS))
        gate(report, "parent_training_members_csv_sha256", file_sha256(member_path), config["parent_bindings"]["training_members_csv_sha256"])
        persistence = physical_persistence_reference(cohort)
        gate(report, "cached_persistence_reference", normalized_result(persistence),
             normalized_result(persistence_subset(config["cached_parent_prediction_metrics"])))
        report["recomputed_persistence_reference"] = persistence
        stage = "frozen_control_execution"
        control = frozen_candidates(config)
        joined = cohort.set_index("market_id")
        for row in control.itertuples(index=False):
            if row.market_id not in joined.index:
                raise ValueError("control_candidate_not_in_bound_cohort")
            original = joined.loc[row.market_id]
            for field in CANDIDATE_FIELDS[1:]:
                if field == "predicted_exit_bid_price":
                    continue
                if getattr(row, field) != original[field]:
                    raise ValueError("control_candidate_prefix_binding_mismatch")
        base.storage._private_csv(output / "frozen-control-candidates.csv", pd.DataFrame(config["frozen_control_candidates"], columns=CANDIDATE_FIELDS))
        control_results = replay_arm(control, quotes, parent)
        report["replayed_cells"] = len(control_results)
        report["arms"]["frozen_control"] = {"candidate_rows": len(control), "prediction_basis": config["cached_parent_prediction_reference_kind"], "results": control_results}
        gate(report, "control_result_count", len(control_results), config["expected_control_result_count"])
        gate(report, "full_control_result_fingerprints", [result_fingerprint(cell) for cell in control_results], config["expected_control_result_fingerprints"])
        report["control_execution_reproduced"] = True
        report["frozen_control_candidate_rows_reused"] = len(control)
        report["cached_parent_prediction_metrics"] = config["cached_parent_prediction_metrics"]
        report["parent_prediction_reference_boundary"] = "Input contains audited cached metrics and artifact hashes, not the full parent score/model artifact bytes; parent control model is neither fitted nor rescored here."
        stage = "treatment_fitting"
        predictions = []
        models = []
        for plan in plans:
            reference = plan["reference"]
            record = {**reference, "scored_rows": 0, "actual_model_fit_calls": 0, "actual_model_predict_calls": 0}
            report["fit_records"].append(record)
            if not plan["eligible"]:
                continue
            if (report["actual_model_fit_calls"] >= config["max_new_model_fit_calls"]
                    or report["actual_model_predict_calls"] >= config["max_new_model_predict_calls"]):
                raise ValueError("treatment_model_budget_exceeded")
            try:
                scored, model = fit_predict_once(frame.loc[plan["train_index"]].copy(), frame.loc[plan["test_index"]].copy(), parent)
            except Exception as exc:
                record.update(status="model_call_failed", actual_model_fit_calls=int(getattr(exc, "audit_fit_calls", 0)),
                              actual_model_predict_calls=int(getattr(exc, "audit_predict_calls", 0)))
                report["actual_model_fit_calls"] += record["actual_model_fit_calls"]
                report["actual_model_predict_calls"] += record["actual_model_predict_calls"]
                raise
            record.update(status="fit_with_prior_available_labels", scored_rows=len(scored), actual_model_fit_calls=1, actual_model_predict_calls=1)
            report["actual_model_fit_calls"] += 1
            report["actual_model_predict_calls"] += 1
            scored["fit_run"] = reference["run_name"]
            predictions.append(scored)
            models.append({"fit_run": reference["run_name"], "cutoff_ts": reference["decision_cutoff_ts"], **model})
            report.update(model_scored_rows=report["model_scored_rows"] + len(scored), fitted_scored_runs=len(models))
            base.storage._private_csv(output / "scores.csv", scored, append=True)
            base.storage._private_json(output / "fitted-models.json", models)
        scored = pd.concat(predictions, ignore_index=True)
        report.update(model_scored_rows=len(scored), fitted_scored_runs=len(models))
        stage = "treatment_scored_cohort"
        gate(report, "treatment_scored_cohort", cohort_contract(scored), config["expected_scored_cohort"])
        stage = "treatment_execution"
        candidates, decisions = base.decision_cost_candidates(scored, parent)
        base.storage._private_csv(output / "side-interaction-decisions.csv", decisions)
        base.storage._private_csv(output / "side-interaction-candidates.csv", candidates)
        treatment_results = replay_arm(candidates, quotes, parent)
        report["replayed_cells"] += len(treatment_results)
        if report["replayed_cells"] > config["max_replay_cells"]:
            raise ValueError("replay_cell_budget_exceeded")
        report["arms"]["side_interaction"] = {"evaluation_cohort_rows": len(scored), "candidate_rows": len(candidates),
            "decision_cost_floor_rejections": len(scored) - len(candidates), "prediction_basis": "side_sign_times_oriented_ridge_residual", "results": treatment_results}
        metrics = base.prediction_metrics(scored)
        report["prediction_metrics"] = metrics
        report["predictive_comparison"] = predictive_comparison(metrics, config["cached_parent_prediction_metrics"], scored)
        report["cash_turnover"] = {"frozen_control": turnover_summary(control_results, parent["initial_cash"]),
                                   "side_interaction": turnover_summary(treatment_results, parent["initial_cash"])}
        report["opportunity_sufficiency"] = {"candidate_rows": len(candidates), "required_entries": parent["required_fills"],
                                             "cardinality_sufficient": len(candidates) >= parent["required_fills"]}
        report.update(evaluation_status="evaluated", strategy_evaluated=bool(len(scored) and models), comparison_valid=True,
            run_order=parent["run_order"], test_runs=parent["run_order"][parent["minimum_training_runs"]:],
            evaluation_blocks=base.disjoint_blocks(parent["run_order"][parent["minimum_training_runs"]:], parent["evaluation_block_runs"]))
    except Exception as exc:
        report.update(evaluation_status="evaluation_failed" if report["control_execution_reproduced"] else "comparison_invalid",
                      strategy_evaluated=False, comparison_valid=False,
                      failure={"stage": stage, "exception_type": type(exc).__name__, "reason": str(exc)})
    report["artifacts"] = {path.name: {"sha256": file_sha256(path), "bytes": path.stat().st_size}
                           for path in sorted(output.iterdir()) if path.is_file()}
    base.storage._private_json(output / "summary.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.config.stat().st_size > 262144:
        raise ValueError("private_input_budget_exceeded")
    report = evaluate(args.source_root, json.loads(args.config.read_text()), args.output)
    return 0 if report["strategy_evaluated"] and report["comparison_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
