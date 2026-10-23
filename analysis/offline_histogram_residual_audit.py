"""Private paired histogram audit with immutable parent data and execution.

Only the new estimator is fitted. Parent predictions are an audited cache;
complete parent execution fingerprints must reproduce before model construction.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.preprocessing import StandardScaler

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_side_interaction_audit as parent_audit

base = parent_audit.base
FEATURES = parent_audit.FEATURES
CANDIDATE_FIELDS = parent_audit.CANDIDATE_FIELDS
MEMBER_FIELDS = parent_audit.MEMBER_FIELDS
FINGERPRINT_VERSION = parent_audit.FINGERPRINT_VERSION
UNCHANGED_FILES = parent_audit.UNCHANGED_FILES | {"analysis/offline_side_interaction_audit.py"}
canonical = parent_audit.canonical
file_sha256 = parent_audit.file_sha256
normalized_result = parent_audit.normalized_result
result_fingerprint = parent_audit.result_fingerprint
cohort_contract = parent_audit.cohort_contract
frozen_candidates = parent_audit.frozen_candidates
verified_inputs = parent_audit.verified_inputs
build_fit_plan = parent_audit.build_fit_plan
replay_arm = parent_audit.replay_arm
gate = parent_audit.gate

MODEL_PARAMETERS = {"categorical_features": None, "early_stopping": False, "interaction_cst": None,
    "l2_regularization": 0.0, "learning_rate": 0.1, "loss": "squared_error", "max_bins": 255,
    "max_depth": 3, "max_features": 1.0, "max_iter": 100, "max_leaf_nodes": 8,
    "min_samples_leaf": 100, "monotonic_cst": None, "n_iter_no_change": 10, "quantile": None,
    "random_state": 0, "scoring": "loss", "tol": 1e-7, "validation_fraction": None,
    "verbose": 0, "warm_start": False}
PACKAGE_MODULES = {"joblib": "joblib", "numpy": "numpy", "pandas": "pandas",
    "scikit-learn": "sklearn", "scipy": "scipy", "threadpoolctl": "threadpoolctl"}
IMPLEMENTATION_MODULES = ["gradient_boosting", "predictor", "grower", "binning", "common",
    "_predictor", "_binning", "splitting", "histogram", "_gradient_boosting"]
NODE_FIELDS = ["value", "count", "feature_idx", "num_threshold", "missing_go_to_left", "left", "right",
    "gain", "depth", "is_leaf", "bin_threshold", "is_categorical", "bitset_idx"]
SPECIAL_FIELDS = {"num_threshold", "gain"}


MODEL_ARTIFACT_CONTRACT = {"format": "native_numeric_histogram_tree_json_v1", "expected_n_iter_per_fit": 100,
    "expected_total_trees_per_fit": 100, "expected_trees_per_iteration": 1,
    "max_trees_per_fit": 100, "max_depth_per_tree": 3, "max_leaves_per_tree": 8, "max_nodes_per_tree": 15,
    "leaf_value_learning_rate_already_applied": True, "numeric_threshold_comparison": "less_than_or_equal_goes_left",
    "tree_accumulation": "baseline_plus_leaf_values_in_original_iteration_order_float64",
    "raw_prediction_mapping": "baseline_plus_sum_tree_values_already_learning_rate_scaled_then_side_sign",
    "bin_thresholds_scaler_baseline_leaf_values_must_be_finite": True,
    "must_export_scaler_baseline_raw_numeric_threshold_nodes": True,
    "must_export_train_only_bin_thresholds_and_effective_parameters": True,
    "must_independently_reconstruct_all_scored_predictions_without_estimator_predict": True,
    "no_pickle_or_executable_payloads": True, "non_numeric_or_categorical_splits_allowed": False,
    "special_float_encoding": {"allowed_fields": ["node.num_threshold", "node.gain"], "nan_allowed": False,
        "negative_infinity": {"special_float": "negative_infinity"}, "positive_infinity": {"special_float": "positive_infinity"}}}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("evaluation_mode") != "paired_histogram_residual":
        raise ValueError("histogram_residual_mode_required")
    prior, protocol = config.get("parent_protocol"), config.get("base_protocol")
    if not isinstance(prior, dict) or not isinstance(protocol, dict):
        raise ValueError("immutable_parent_and_base_protocol_required")
    parent_audit.validate_config(prior)
    bindings = config.get("parent_bindings", {})
    for key in ("expected_code_commit", "source_commit", "parent_code_commit"):
        if not re.fullmatch(r"[a-f0-9]{40}", str(config.get(key, ""))):
            raise ValueError("invalid_commit_binding")
    if (config["source_commit"] != prior["source_commit"] or protocol != prior["base_protocol"]
            or config["parent_code_commit"] != prior["expected_code_commit"]
            or hashlib.sha256(canonical(prior)).hexdigest() != bindings.get("protocol_sha256")
            or hashlib.sha256(canonical(protocol)).hexdigest() != bindings.get("base_protocol_sha256")):
        raise ValueError("parent_protocol_binding_mismatch")
    for field in ("summary_sha256", "protocol_sha256", "base_protocol_sha256", "features_csv_sha256",
                  "scores_csv_sha256", "training_members_csv_sha256", "model_parameters_sha256", "candidates_csv_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(bindings.get(field, ""))):
            raise ValueError("missing_parent_artifact_binding")
    unchanged = bindings.get("unchanged_code_file_sha256", {})
    if set(unchanged) != UNCHANGED_FILES or any(not re.fullmatch(r"[a-f0-9]{64}", str(v)) for v in unchanged.values()):
        raise ValueError("immutable_six_helper_bindings_required")
    for name, value in prior["parent_bindings"]["unchanged_code_file_sha256"].items():
        if unchanged[name] != value:
            raise ValueError("ancestor_helper_binding_mismatch")
    treatment = config.get("treatment", {})
    preserved = ("side_sign", "training_target", "raw_model_output", "physical_residual_prediction",
                 "physical_future_bid_prediction", "scaler")
    if (any(treatment.get(k) != prior["treatment"][k] for k in preserved)
            or treatment.get("intercept") != "fit_and_invert_entire_histogram_baseline_and_tree_sum_with_same_side_sign"
            or treatment.get("model") != {"estimator": "sklearn.ensemble.HistGradientBoostingRegressor", "parameters": MODEL_PARAMETERS}):
        raise ValueError("fixed_histogram_model_contract_required")
    if (config.get("control_model_refit_allowed") is not False or config.get("control_model_predict_allowed") is not False
            or config.get("live_orders_enabled") is not False or config.get("actual_fills") != 0
            or config.get("verified_canary_fills") != 0):
        raise ValueError("offline_cached_control_contract_required")
    if config.get("control_result_fingerprint_version") != FINGERPRINT_VERSION:
        raise ValueError("control_fingerprint_version_mismatch")
    for field in ("max_new_model_fit_calls", "max_new_model_predict_calls", "max_replay_cells", "max_private_config_bytes",
                  "expected_control_candidate_count", "expected_control_result_count"):
        if type(config.get(field)) is not int or config[field] < 1:
            raise ValueError("invalid_budget_or_reference_count")
    if config["max_private_config_bytes"] > 262144 or len(canonical(config)) > config["max_private_config_bytes"]:
        raise ValueError("private_input_budget_exceeded")
    runs = protocol["run_order"][protocol["minimum_training_runs"]:]
    cells = 1 + len(base.disjoint_blocks(runs, protocol["evaluation_block_runs"]))
    if (config["expected_control_result_count"] != cells or 2 * cells > config["max_replay_cells"]
            or len(runs) > config["max_new_model_fit_calls"] or len(runs) > config["max_new_model_predict_calls"]):
        raise ValueError("computation_budget_or_scope_mismatch")
    expected = config.get("expected_control_result_fingerprints")
    if not isinstance(expected, list) or len(expected) != cells or any(not re.fullmatch(r"[a-f0-9]{64}", str(v)) for v in expected):
        raise ValueError("complete_control_fingerprints_required")
    records = config.get("expected_parent_fit_records")
    if records != prior["expected_parent_fit_records"]:
        raise ValueError("same_causal_fit_plan_required")
    for field in ("expected_scored_cohort", "expected_scored_cohort_per_run", "cached_parent_prediction_reference_kind"):
        if config.get(field) != prior[field]:
            raise ValueError("same_scored_cohort_and_cache_boundary_required")
    env = config.get("environment", {})
    if (set(env.get("packages", {})) != set(PACKAGE_MODULES)
            or any(not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(v)) for v in env["packages"].values())
            or not re.fullmatch(r"[0-9]+\.[0-9]+", str(env.get("python_major_minor", "")))
            or any(env.get(k) is not True for k in ("must_assert_effective_get_params_exact_before_fit",
                "must_assert_runtime_versions_before_model_construction", "must_record_loaded_histogram_implementation_module_file_hashes"))):
        raise ValueError("complete_runtime_binding_required")
    contract = config.get("model_artifact_contract", {})
    required = MODEL_ARTIFACT_CONTRACT
    if contract != required:
        raise ValueError("fixed_native_tree_artifact_contract_required")
    frozen_candidates(config)
    return config


def assert_runtime_environment(config: dict[str, Any]) -> dict[str, Any]:
    expected = config["environment"]
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    versions, loaded = {}, {}
    for distribution, module_name in PACKAGE_MODULES.items():
        versions[distribution] = importlib.metadata.version(distribution)
        loaded[distribution] = str(importlib.import_module(module_name).__version__)
    if python != expected["python_major_minor"] or versions != expected["packages"] or loaded != versions:
        raise ValueError("runtime_version_binding_mismatch")
    return {"python_major_minor": python, "packages": versions, "loaded_module_versions": loaded}


def implementation_module_hashes() -> list[dict[str, str]]:
    evidence = []
    for suffix in IMPLEMENTATION_MODULES:
        name = "sklearn.ensemble._hist_gradient_boosting." + suffix
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        evidence.append({"module": name, "path": str(path), "sha256": file_sha256(path)})
    return evidence


def finite_array(value: Any, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if not np.isfinite(result).all():
        raise ValueError("nonfinite_" + name)
    return result


def encode_node_value(value: Any, field: str) -> Any:
    if isinstance(value, (np.integer, int)):
        return int(value)
    result = float(value)
    if math.isfinite(result):
        return result
    if math.isnan(result) or field not in SPECIAL_FIELDS:
        raise ValueError("nonfinite_node_" + field)
    return {"special_float": "positive_infinity" if result > 0 else "negative_infinity"}


def decode_node_value(value: Any, field: str) -> float:
    if isinstance(value, dict):
        if field not in SPECIAL_FIELDS or value not in ({"special_float": "positive_infinity"}, {"special_float": "negative_infinity"}):
            raise ValueError("invalid_special_float_tag")
        return math.inf if value["special_float"] == "positive_infinity" else -math.inf
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("untagged_nonfinite_node_value")
    return result


def validate_serialized_tree(tree: dict[str, Any], feature_count: int, contract: dict[str, Any]) -> None:
    nodes = tree["nodes"]
    if not 1 <= len(nodes) <= contract["max_nodes_per_tree"]:
        raise ValueError("tree_node_budget_exceeded")
    visited, stack, leaves = set(), [(0, 0)], 0
    while stack:
        index, depth = stack.pop()
        if index in visited or not 0 <= index < len(nodes):
            raise ValueError("invalid_tree_topology")
        visited.add(index)
        node = nodes[index]
        if set(node) != set(NODE_FIELDS) or node["depth"] != depth or depth > contract["max_depth_per_tree"]:
            raise ValueError("invalid_tree_schema_or_depth")
        for field, value in node.items():
            decode_node_value(value, field)
        if node["is_categorical"] != 0 or node["is_leaf"] not in (0, 1) or node["missing_go_to_left"] not in (0, 1):
            raise ValueError("unsupported_tree_node_kind")
        if node["is_leaf"]:
            leaves += 1
        else:
            if type(node["feature_idx"]) is not int or not 0 <= node["feature_idx"] < feature_count:
                raise ValueError("invalid_split_feature")
            if type(node["left"]) is not int or type(node["right"]) is not int:
                raise ValueError("invalid_child_index")
            stack.extend([(node["right"], depth + 1), (node["left"], depth + 1)])
    if len(visited) != len(nodes) or leaves > contract["max_leaves_per_tree"]:
        raise ValueError("unreachable_nodes_or_leaf_budget_exceeded")


def serialize_histogram_model(model: Any, scaler: Any, config: dict[str, Any]) -> dict[str, Any]:
    contract = config["model_artifact_contract"]
    params = model.get_params(deep=False)
    if params != config["treatment"]["model"]["parameters"]:
        raise ValueError("effective_histogram_parameters_mismatch")
    mean, scale = finite_array(scaler.mean_, "scaler_mean"), finite_array(scaler.scale_, "scaler_scale")
    if mean.shape != (len(FEATURES),) or scale.shape != mean.shape or not (scale > 0).all():
        raise ValueError("invalid_scaler_shape_or_scale")
    baseline = finite_array(model._baseline_prediction, "baseline_prediction")
    if baseline.size != 1:
        raise ValueError("scalar_regression_baseline_required")
    iterations, per_iteration = int(model.n_iter_), int(model.n_trees_per_iteration_)
    if iterations != contract["expected_n_iter_per_fit"] or per_iteration != contract["expected_trees_per_iteration"]:
        raise ValueError("fixed_iteration_count_mismatch")
    trees = []
    for iteration, predictors in enumerate(model._predictors):
        if len(predictors) != per_iteration:
            raise ValueError("tree_count_per_iteration_mismatch")
        for tree_index, predictor in enumerate(predictors):
            if list(predictor.nodes.dtype.names) != NODE_FIELDS:
                raise ValueError("unexpected_native_node_dtype")
            if np.asarray(predictor.binned_left_cat_bitsets).size or np.asarray(predictor.raw_left_cat_bitsets).size:
                raise ValueError("categorical_bitsets_not_allowed")
            tree = {"iteration": iteration, "tree_index": tree_index,
                "node_dtype": [{"name": name, "dtype": predictor.nodes.dtype.fields[name][0].str} for name in NODE_FIELDS],
                "nodes": [{field: encode_node_value(node[field], field) for field in NODE_FIELDS} for node in predictor.nodes]}
            validate_serialized_tree(tree, len(FEATURES), contract)
            trees.append(tree)
    if len(model._predictors) != iterations or len(trees) != contract["expected_total_trees_per_fit"]:
        raise ValueError("fixed_total_tree_count_mismatch")
    mapper = model._bin_mapper
    thresholds = [finite_array(values, "bin_thresholds").tolist() for values in mapper.bin_thresholds_]
    counts = np.asarray(mapper.n_bins_non_missing_)
    categorical = np.asarray(mapper.is_categorical_)
    if len(thresholds) != len(FEATURES) or counts.shape != (len(FEATURES),) or categorical.shape != counts.shape or categorical.any():
        raise ValueError("invalid_numeric_bin_mapper")
    for values, count in zip(thresholds, counts):
        if len(values) + 1 != int(count) or not np.all(np.diff(values) > 0):
            raise ValueError("invalid_bin_threshold_order_or_count")
    if mapper.known_categories is not None:
        raise ValueError("known_categories_not_allowed")
    samples = finite_array(scaler.n_samples_seen_, "scaler_sample_count")
    if samples.size != 1 or float(samples.item()) <= 0 or float(samples.item()) != int(samples.item()):
        raise ValueError("invalid_scaler_sample_count")
    return {"format": contract["format"], "scaler_mean": mean.tolist(), "scaler_scale": scale.tolist(),
        "scaler_n_samples_seen": int(samples.item()),
        "orientation": {"side_sign": {"up": 1, "down": -1}, "training_target": "side_sign * physical_bid_residual_target",
            "prediction": "physical_residual = side_sign * oriented_model_output", "whole_model_inverted_with_side": True},
        "histogram_model": {"estimator": config["treatment"]["model"]["estimator"], "effective_parameters": params,
            "features": FEATURES.copy(), "baseline_prediction": float(baseline.item()), "n_iter": iterations,
            "n_trees_per_iteration": per_iteration, "trees": trees,
            "bin_mapper": {"bin_thresholds": thresholds, "n_bins_non_missing": counts.astype(int).tolist(),
                "missing_values_bin_idx": int(mapper.missing_values_bin_idx_), "is_categorical": categorical.astype(int).tolist(),
                "subsample": mapper.subsample, "n_bins": mapper.n_bins, "known_categories": None},
            "implementation_modules": implementation_module_hashes()}}


def reconstruct_serialized_histogram(artifact: dict[str, Any], X: Any) -> np.ndarray:
    """Traverse saved numeric nodes on original feature values, without sklearn."""
    values = finite_array(X, "reconstruction_inputs")
    if values.ndim != 2 or values.shape[1] != len(FEATURES):
        raise ValueError("invalid_reconstruction_feature_shape")
    mean = finite_array(artifact["scaler_mean"], "saved_scaler_mean")
    scale = finite_array(artifact["scaler_scale"], "saved_scaler_scale")
    if mean.shape != (len(FEATURES),) or scale.shape != mean.shape or not (scale > 0).all():
        raise ValueError("invalid_saved_scaler")
    values = (values - mean) / scale
    if not np.isfinite(values).all():
        raise ValueError("nonfinite_scaled_reconstruction_inputs")
    saved = artifact["histogram_model"]
    if saved["features"] != FEATURES:
        raise ValueError("saved_feature_order_mismatch")
    predictions = np.full(len(values), float(saved["baseline_prediction"]), dtype=np.float64)
    if not np.isfinite(predictions).all():
        raise ValueError("nonfinite_saved_baseline")
    for tree in saved["trees"]:
        nodes = tree["nodes"]
        for row_index, row in enumerate(values):
            node_index, seen = 0, set()
            while True:
                if node_index in seen or not 0 <= node_index < len(nodes):
                    raise ValueError("invalid_saved_tree_topology")
                seen.add(node_index)
                node = nodes[node_index]
                if node["is_categorical"]:
                    raise ValueError("categorical_reconstruction_not_allowed")
                if node["is_leaf"]:
                    predictions[row_index] += decode_node_value(node["value"], "value")
                    break
                threshold = decode_node_value(node["num_threshold"], "num_threshold")
                node_index = node["left"] if row[node["feature_idx"]] <= threshold else node["right"]
    if not np.isfinite(predictions).all():
        raise ValueError("nonfinite_reconstructed_prediction")
    return predictions


def fit_predict_once(train: pd.DataFrame, current: pd.DataFrame,
                     config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    fit_calls = predict_calls = 0
    try:
        environment = assert_runtime_environment(config)
        train_sign, test_sign = parent_audit.side_sign(train), parent_audit.side_sign(current)
        train_X, test_X = finite_array(train[FEATURES], "training_features"), finite_array(current[FEATURES], "test_features")
        target = train_sign * finite_array(train["bid_residual_target"], "training_target")
        params = config["treatment"]["model"]["parameters"]
        if params != MODEL_PARAMETERS:
            raise ValueError("fixed_histogram_model_contract_required")
        model = HistGradientBoostingRegressor(**params)
        if model.get_params(deep=False) != params:
            raise ValueError("effective_histogram_parameters_mismatch")
        scaler = StandardScaler()
        transformed = scaler.fit_transform(train_X)
        fit_calls = 1
        model.fit(transformed, target)
        transformed_test = scaler.transform(test_X)
        predict_calls = 1
        oriented = finite_array(model.predict(transformed_test), "oriented_prediction")
        if oriented.shape != (len(current),):
            raise ValueError("invalid_prediction_shape")
        artifact = serialize_histogram_model(model, scaler, config)
        artifact["runtime_environment"] = environment
        reconstructed = reconstruct_serialized_histogram(artifact, test_X)
        if not np.allclose(reconstructed, oriented, rtol=1e-12, atol=1e-12):
            raise ValueError("saved_tree_prediction_reconstruction_mismatch")
        artifact["prediction_reconstruction"] = {"rows": len(current), "rtol": 1e-12, "atol": 1e-12,
            "maximum_absolute_difference": float(np.max(np.abs(reconstructed - oriented))) if len(current) else 0.0}
        scored = current.copy()
        scored["predicted_oriented_residual_raw"] = oriented
        scored["predicted_bid_residual_raw"] = test_sign * oriented
        scored["predicted_exit_bid_unclipped"] = scored["decision_bid_price"] + scored["predicted_bid_residual_raw"]
        scored["predicted_exit_bid_price"] = scored["predicted_exit_bid_unclipped"].clip(0, 1)
        canonical(artifact)
        return scored, artifact
    except Exception as exc:
        exc.audit_fit_calls = fit_calls
        exc.audit_predict_calls = predict_calls
        raise


def append_private_jsonl(path: Path, value: dict[str, Any]) -> None:
    encoded = canonical(value) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(encoded)
    path.chmod(0o600)


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    report = {"format": "offline-histogram-residual-audit-v1", "evaluation_status": "comparison_invalid",
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
        persistence = parent_audit.physical_persistence_reference(cohort)
        gate(report, "cached_persistence_reference", normalized_result(persistence),
             normalized_result(parent_audit.persistence_subset(config["cached_parent_prediction_metrics"])))
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
        stage = "runtime_environment"
        report["runtime_environment"] = assert_runtime_environment(config)
        report["implementation_modules"] = implementation_module_hashes()
        stage = "treatment_fitting"
        predictions = []
        fitted_count = 0
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
                scored, model = fit_predict_once(frame.loc[plan["train_index"]].copy(), frame.loc[plan["test_index"]].copy(), config)
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
            fitted_count += 1
            report.update(model_scored_rows=report["model_scored_rows"] + len(scored), fitted_scored_runs=fitted_count)
            append_private_jsonl(output / "fitted-models.jsonl", {"fit_run": reference["run_name"],
                "cutoff_ts": reference["decision_cutoff_ts"], "training_rows": reference["training_rows"],
                "training_known_runs": reference["training_known_runs"],
                "training_market_ids_sha256": reference["training_market_ids_sha256"], **model})
            base.storage._private_csv(output / "scores.csv", scored, append=True)
        scored = pd.concat(predictions, ignore_index=True)
        report.update(model_scored_rows=len(scored), fitted_scored_runs=fitted_count)
        stage = "treatment_scored_cohort"
        gate(report, "treatment_scored_cohort", cohort_contract(scored), config["expected_scored_cohort"])
        stage = "treatment_execution"
        candidates, decisions = base.decision_cost_candidates(scored, parent)
        base.storage._private_csv(output / "histogram-residual-decisions.csv", decisions)
        base.storage._private_csv(output / "histogram-residual-candidates.csv", candidates)
        treatment_results = replay_arm(candidates, quotes, parent)
        report["replayed_cells"] += len(treatment_results)
        if report["replayed_cells"] > config["max_replay_cells"]:
            raise ValueError("replay_cell_budget_exceeded")
        report["arms"]["histogram_residual"] = {"evaluation_cohort_rows": len(scored), "candidate_rows": len(candidates),
            "decision_cost_floor_rejections": len(scored) - len(candidates), "prediction_basis": "side_sign_times_oriented_histogram_residual", "results": treatment_results}
        metrics = base.prediction_metrics(scored)
        report["prediction_metrics"] = metrics
        report["predictive_comparison"] = parent_audit.predictive_comparison(metrics, config["cached_parent_prediction_metrics"], scored)
        report["cash_turnover"] = {"frozen_control": parent_audit.turnover_summary(control_results, parent["initial_cash"]),
                                   "histogram_residual": parent_audit.turnover_summary(treatment_results, parent["initial_cash"])}
        report["opportunity_sufficiency"] = {"candidate_rows": len(candidates), "required_entries": parent["required_fills"],
                                             "cardinality_sufficient": len(candidates) >= parent["required_fills"]}
        report.update(evaluation_status="evaluated", strategy_evaluated=bool(len(scored) and fitted_count and report["control_execution_reproduced"]), comparison_valid=True,
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
