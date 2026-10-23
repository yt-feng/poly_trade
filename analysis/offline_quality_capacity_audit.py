"""Private, cache-only causal quality-label and exit-capacity comparison.

Every evaluation opportunity is retained. Future quote quality changes only past
training membership; the capacity model consumes a fixed decision-time whitelist.
The control must reproduce before either estimator is constructed.
"""
from __future__ import annotations

import argparse
import copy
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
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.preprocessing import StandardScaler

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_histogram_residual_audit as histogram
import archived_quote_cash_domain_export as domains_export

base = histogram.base
parent = histogram.parent_audit
FEATURES = histogram.FEATURES
CAPACITY_FEATURES = [*FEATURES, "side_sign"]
PRICE_PARAMETERS = copy.deepcopy(histogram.MODEL_PARAMETERS)
CAPACITY_PARAMETERS = {**PRICE_PARAMETERS, "loss": "quantile", "quantile": .1}
UNCHANGED_FILES = domains_export.UNCHANGED_FILES
CONTROL_COLUMNS = ["market_id", "run_name", "fit_run", "predicted_oriented_residual_raw", "predicted_bid_residual_raw",
                   "predicted_exit_bid_unclipped", "predicted_exit_bid_price"]
MODEL_ARTIFACT_CONTRACT = {**histogram.MODEL_ARTIFACT_CONTRACT,
    "raw_prediction_mapping": "baseline_plus_sum_tree_values_already_learning_rate_scaled_then_registered_output_map"}
MODEL_CONTRACT = {"price_features": FEATURES, "capacity_features": CAPACITY_FEATURES,
    "price_target": "side_sign_times_raw_bid_residual", "capacity_target": "log1p_depth_haircut_times_exit_available_shares",
    "capacity_prediction": "maximum_zero_expm1_raw_output", "capacity_quantile": .1,
    "capacity_constant_baseline": "numpy_quantile_inverted_cdf_of_training_target_at_fixed_quantile",
    "quality_label_gate": "simulation_cash_domain_observed_and_consistent_parent_target_clock",
    "training_membership": "previous_source_runs_and_actual_exit_strictly_before_current_minimum_decision",
    "insufficient_training": "retain_all_test_rows_nullable_forecasts_no_backfill",
    "training_plan_shared_between_estimators": True}
MEMBER_FIELDS = ["fit_run", "market_id", "run_name", "label_available_ts", "bid_residual_target",
    "capacity_log_target", "decision_cutoff_ts"]
PREDICTION_FIELDS = ["fit_run", "model_status", "predicted_oriented_residual_raw", "predicted_bid_residual_raw",
    "predicted_exit_bid_unclipped", "predicted_exit_bid_price", "predicted_capacity_log_raw",
    "predicted_exit_capacity_shares", "capacity_constant_log_baseline"]
canonical = histogram.canonical
file_sha256 = histogram.file_sha256
cohort_contract = histogram.cohort_contract
result_fingerprint = histogram.result_fingerprint
append_private_jsonl = histogram.append_private_jsonl
assert_runtime_environment = histogram.assert_runtime_environment
implementation_module_hashes = histogram.implementation_module_hashes


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("evaluation_mode") != "cached_quality_capacity":
        raise ValueError("cached_quality_capacity_mode_required")
    protocol = base.validate_config(config["base_protocol"])
    if (not domains_export._valid_digest(config.get("expected_code_commit"), 40)
            or config.get("source_commit") != protocol["source_commit"]
            or digest(protocol) != config.get("frozen_base_protocol_sha256")):
        raise ValueError("frozen_base_protocol_binding_mismatch")
    frozen = config.get("parent_bindings", {}).get("unchanged_code_file_sha256", {})
    if set(frozen) != UNCHANGED_FILES or any(not domains_export._valid_digest(v) for v in frozen.values()):
        raise ValueError("complete_frozen_helper_binding_required")
    bindings = config.get("cache_bindings", {})
    for name in ("features_csv_sha256", "domains_csv_sha256", "control_predictions_csv_sha256"):
        if not domains_export._valid_digest(bindings.get(name)):
            raise ValueError("complete_cache_byte_bindings_required")
    private_inputs = config.get("private_inputs")
    if private_inputs is not None:
        if not isinstance(private_inputs, dict) or set(private_inputs) != {"dataset", "control"}:
            raise ValueError("invalid_private_input_binding")
        for record in private_inputs.values():
            if not isinstance(record, dict):
                raise ValueError("invalid_private_input_binding")
            source = record.get("source", {})
            if (not domains_export._valid_digest(record.get("descriptor_sha256"))
                    or set(source) != {"repository", "commit", "path"}
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", str(source.get("repository", "")))
                    or source["repository"].rsplit("/", 1)[1] in {".", ".."}
                    or not domains_export._valid_digest(source.get("commit"), 40)
                    or not isinstance(source.get("path"), str)
                    or not re.fullmatch(r"research_vault/episodes/[0-9a-f]{64}/part-0000[.]vault", source["path"])):
                raise ValueError("invalid_private_input_binding")
    if (config.get("price_model_parameters") != PRICE_PARAMETERS or config.get("capacity_model_parameters") != CAPACITY_PARAMETERS
            or config.get("model_artifact_contract") != MODEL_ARTIFACT_CONTRACT or config.get("model_contract") != MODEL_CONTRACT):
        raise ValueError("fixed_model_and_feature_contract_required")
    for field in ("control_model_refit_allowed", "control_model_predict_allowed", "live_orders_enabled", "new_data_collection_allowed"):
        if config.get(field) is not False:
            raise ValueError("offline_cached_control_required")
    for field in ("actual_fills", "verified_canary_fills"):
        if type(config.get(field)) is not int or config[field] != 0:
            raise ValueError("zero_actual_fill_contract_required")
    runs = protocol["run_order"][protocol["minimum_training_runs"]:]
    scopes = 1 + len(base.disjoint_blocks(runs, protocol["evaluation_block_runs"]))
    exact = {"max_price_fit_calls": len(runs), "max_capacity_fit_calls": len(runs),
        "max_new_model_fit_calls": 2 * len(runs), "max_new_model_predict_calls": 2 * len(runs),
        "max_replay_cells": 3 * scopes, "expected_control_result_count": scopes, "max_new_cloud_runs": 1,
        "max_private_config_bytes": 262144}
    if any(type(config.get(k)) is not int or config[k] != v for k, v in exact.items()):
        raise ValueError("exact_computation_budget_required")
    if len(canonical(config)) > config["max_private_config_bytes"]:
        raise ValueError("private_input_budget_exceeded")
    for field in ("expected_all_cohort", "expected_test_cohort"):
        domains_export._validate_cohort(config.get(field))
    per_run = config.get("expected_test_cohort_per_run", {})
    if set(per_run) != set(runs):
        raise ValueError("complete_test_run_cohort_required")
    for value in per_run.values():
        domains_export._validate_cohort(value)
    if config.get("control_result_fingerprint_version") != parent.FINGERPRINT_VERSION:
        raise ValueError("complete_control_fingerprint_required")
    prints = config.get("expected_control_result_fingerprints")
    if not isinstance(prints, list) or len(prints) != scopes or any(not domains_export._valid_digest(v) for v in prints):
        raise ValueError("complete_control_fingerprint_required")
    parent.frozen_candidates(config)
    env = config.get("environment", {})
    if (set(env.get("packages", {})) != set(histogram.PACKAGE_MODULES)
            or not re.fullmatch(r"\d+\.\d+", str(env.get("python_major_minor", "")))
            or any(not re.fullmatch(r"\d+\.\d+\.\d+", str(v)) for v in env["packages"].values())
            or any(env.get(k) is not True for k in ("must_assert_effective_get_params_exact_before_fit",
                "must_assert_runtime_versions_before_model_construction", "must_record_loaded_histogram_implementation_module_file_hashes"))):
        raise ValueError("complete_runtime_binding_required")
    return config


def verified_inputs(config: dict[str, Any]) -> dict[str, Any]:
    code = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=CURRENT_DIR.parent, text=True).strip()
    if code != config["expected_code_commit"]:
        raise ValueError("code_commit_binding_mismatch")
    hashes = {name: file_sha256(CURRENT_DIR.parent / name) for name in sorted(UNCHANGED_FILES)}
    if hashes != config["parent_bindings"]["unchanged_code_file_sha256"]:
        raise ValueError("immutable_helper_bytes_changed")
    return {"code_commit": code, "source_commit": config["source_commit"], "unchanged_code_file_sha256": hashes,
        "source_binding_basis": "audited_immutable_dataset_bytes_and_registered_source_commit_not_raw_source_rescan"}


def _numeric(values: pd.Series, name: str, nullable: bool = False) -> pd.Series:
    if nullable:
        values = values.replace("", np.nan)
    result = values.map(float).astype(float)
    if np.isinf(result).any() or (not nullable and result.isna().any()):
        raise ValueError("invalid_numeric_" + name)
    return result


def _times(values: pd.Series, name: str, nullable: bool = False) -> pd.Series:
    for value in values:
        if value == "" and nullable:
            continue
        if pd.Timestamp(value).tzinfo is None:
            raise ValueError("unqualified_clock_" + name)
    result = pd.to_datetime(values.replace("", None), utc=True, errors="raise", format="mixed")
    if not nullable and result.isna().any():
        raise ValueError("missing_clock_" + name)
    return result


def _bool(values: pd.Series, name: str, nullable: bool = False) -> pd.Series:
    allowed = {"True": True, "False": False}
    if nullable:
        allowed[""] = None
    if not values.isin(allowed).all():
        raise ValueError("invalid_boolean_" + name)
    return values.map(allowed)


def load_inputs(dataset_root: Path, control_root: Path, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    paths = {"features_csv_sha256": dataset_root / "features.csv", "domains_csv_sha256": dataset_root / "quote-cash-domains.csv",
             "control_predictions_csv_sha256": control_root / "control-predictions.csv"}
    for key, path in paths.items():
        if not path.is_file() or path.is_symlink() or file_sha256(path) != config["cache_bindings"][key]:
            raise ValueError("dataset_byte_binding_mismatch_" + key)
    frame, domains, control = [pd.read_csv(paths[k], dtype=str, keep_default_na=False, float_precision="round_trip") for k in paths]
    required = ["market_id", "run_name", "slug", "first_quote_ts", "decision_ts", "market_close_ts", *FEATURES,
        "side", "decision_bid_price", "decision_ask_price", "decision_sell_size", "future_bid_price", "bid_residual_target",
        "label_available_ts", "target_entry_quote_ts", "target_exit_arrival_ts", "target_status"]
    if not set(required).issubset(frame) or list(domains) != domains_export.COLUMNS or list(control) != CONTROL_COLUMNS:
        raise ValueError("dataset_schema_mismatch")
    if (frame.market_id.duplicated().any() or frame.slug.duplicated().any() or domains.market_id.duplicated().any()
            or control.market_id.duplicated().any() or list(frame.market_id) != list(domains.market_id)
            or not frame.market_id.eq(frame.run_name + "::" + frame.slug).all()
            or not frame.side.isin(["up", "down"]).all()):
        raise ValueError("dataset_identity_or_order_mismatch")
    for name in ("run_name", "slug", "side"):
        if list(frame[name]) != list(domains[name]):
            raise ValueError("domain_join_mismatch_" + name)
    for name in ("first_quote_ts", "decision_ts", "market_close_ts", "label_available_ts", "target_entry_quote_ts", "target_exit_arrival_ts"):
        frame[name] = _times(frame[name], name, name in ("label_available_ts", "target_entry_quote_ts", "target_exit_arrival_ts"))
    for name in ("decision_ts", "market_close_ts"):
        if not frame[name].equals(_times(domains[name], name)):
            raise ValueError("domain_join_mismatch_" + name)
    for name in [*FEATURES, "decision_bid_price", "decision_ask_price", "decision_sell_size"]:
        frame[name] = _numeric(frame[name], name)
    for name in ("future_bid_price", "bid_residual_target"):
        frame[name] = _numeric(frame[name], name, True)
    protocol = config["base_protocol"]
    if (not frame.run_name.isin(protocol["run_order"]).all() or not frame.decision_ts.lt(frame.market_close_ts).all()
            or not frame.first_quote_ts.le(frame.decision_ts).all()
            or not frame.decision_bid_price.between(0, 1, inclusive="neither").all()
            or not frame.decision_ask_price.between(0, 1, inclusive="neither").all()
            or not frame.decision_sell_size.ge(0).all()):
        raise ValueError("invalid_prefix_domain_or_clock")
    if list(frame.market_id) != list(frame.sort_values(["decision_ts", "run_name", "slug"], kind="mergesort").market_id):
        raise ValueError("nonchronological_feature_rows")
    observed_runs = list(frame.groupby("run_name", sort=False).groups)
    if observed_runs != [run for run in protocol["run_order"] if run in observed_runs]:
        raise ValueError("nonchronological_source_runs")
    known = frame.future_bid_price.notna()
    if (not frame.loc[known, "future_bid_price"].between(0, 1, inclusive="neither").all()
            or not frame.bid_residual_target.notna().equals(known) or not frame.label_available_ts.notna().equals(known)
            or not np.allclose(frame.loc[known, "bid_residual_target"],
                frame.loc[known, "future_bid_price"] - frame.loc[known, "decision_bid_price"], rtol=0, atol=1e-12)):
        raise ValueError("inconsistent_parent_target")
    quality = []
    capacity = []
    effective_capacity = []
    for index, record in domains.iterrows():
        row = frame.iloc[index]
        if record.parent_target_status != row.target_status:
            raise ValueError("parent_target_status_mismatch")
        label = pd.Timestamp(record.parent_label_available_ts) if record.parent_label_available_ts else pd.NaT
        if not ((pd.isna(label) and pd.isna(row.label_available_ts)) or label == row.label_available_ts):
            raise ValueError("parent_label_clock_mismatch")
        selected = {}
        full_quality = {}
        for kind in ("entry", "exit"):
            selected[kind] = record[kind + "_quote_status"] == "selected"
            if not selected[kind]:
                full_quality[kind] = False
                continue
            stamp = pd.Timestamp(record[kind + "_quote_ts"])
            if stamp.tzinfo is None:
                raise ValueError("unqualified_selected_quote_clock")
            raw = pd.Series({name: float(record[kind + "_" + name]) if record[kind + "_" + name] else np.nan for name in base.BOOK_NUMERIC})
            action, price_name = ("buy", "ask") if kind == "entry" else ("sell", "bid")
            raw_price = raw[f"{action}_{row.side}_cents"] / 100.
            raw_size = raw[f"{action}_{row.side}_size"]
            if not math.isfinite(raw_price) or not 0 < raw_price < 1:
                raise ValueError("invalid_selected_side_price")
            if not math.isclose(float(record[f"{kind}_{price_name}_price"]), raw_price, rel_tol=0, abs_tol=1e-12):
                raise ValueError("selected_price_snapshot_mismatch")
            size = float(record[kind + "_available_shares"]) if record[kind + "_available_shares"] else np.nan
            if not ((pd.isna(size) and pd.isna(raw_size)) or size == raw_size):
                raise ValueError("selected_size_snapshot_mismatch")
            if record[kind + "_size_valid"] not in ("True", "False") or (record[kind + "_size_valid"] == "True") != bool(math.isfinite(size) and size >= 0):
                raise ValueError("selected_size_validity_mismatch")
            full_quality[kind] = bool(base._valid_book(raw))
            if record[kind + "_full_book_valid"] not in ("True", "False") or (record[kind + "_full_book_valid"] == "True") != full_quality[kind]:
                raise ValueError("full_book_quality_mismatch")
        price_pair = selected["entry"] and selected["exit"]
        if price_pair != bool(known.iloc[index]):
            raise ValueError("parent_known_price_pair_mismatch")
        if selected["entry"] and pd.Timestamp(record.entry_quote_ts) != row.target_entry_quote_ts:
            raise ValueError("parent_entry_clock_mismatch")
        if price_pair:
            p_in, p_out = float(record.entry_ask_price) + protocol["slippage"], float(record.exit_bid_price) - protocol["slippage"]
            entry_size = float(record.entry_available_shares) if record.entry_available_shares else np.nan
            exit_size = float(record.exit_available_shares) if record.exit_available_shares else np.nan
            q = bool(full_quality["entry"] and full_quality["exit"] and math.isfinite(entry_size) and entry_size >= 0
                and math.isfinite(exit_size) and exit_size >= 0 and 0 < p_in < 1 and 0 < p_out < 1)
            if (not math.isclose(float(record.exit_bid_price), row.future_bid_price, rel_tol=0, abs_tol=1e-12)
                    or pd.Timestamp(record.exit_quote_ts) != row.label_available_ts
                    or pd.Timestamp(record.exit_target_ts) != row.target_exit_arrival_ts):
                raise ValueError("parent_future_price_or_clock_mismatch")
        else:
            q, exit_size = False, np.nan
        if record.simulation_cash_domain_observed not in ("True", "False") or (record.simulation_cash_domain_observed == "True") != q:
            raise ValueError("simulation_quality_gate_mismatch")
        if q and pd.Timestamp(record.cash_domain_label_available_ts) != row.label_available_ts:
            raise ValueError("quality_label_clock_mismatch")
        quality.append(q)
        effective_capacity.append(protocol["depth_haircut"] * exit_size if q else np.nan)
        capacity.append(math.log1p(protocol["depth_haircut"] * exit_size) if q else np.nan)
    frame["quality_label_observed"] = quality
    frame["capacity_log_target"] = capacity
    frame["future_effective_exit_capacity"] = effective_capacity
    test_runs = protocol["run_order"][protocol["minimum_training_runs"]:]
    cohort = frame[frame.run_name.isin(test_runs)]
    if list(control.market_id) != list(cohort.market_id) or list(control.run_name) != list(cohort.run_name):
        raise ValueError("complete_ordered_control_projection_required")
    if not control.fit_run.eq(control.run_name).all():
        raise ValueError("cached_control_fit_run_mismatch")
    for name in CONTROL_COLUMNS[3:]:
        control[name] = _numeric(control[name], name)
    signed = parent.side_sign(cohort)
    for values, expected in ((control.predicted_bid_residual_raw, signed * control.predicted_oriented_residual_raw),
            (control.predicted_exit_bid_unclipped, cohort.decision_bid_price.to_numpy() + control.predicted_bid_residual_raw.to_numpy()),
            (control.predicted_exit_bid_price, control.predicted_exit_bid_unclipped.clip(0, 1))):
        if not np.allclose(values, expected, rtol=0, atol=1e-12):
            raise ValueError("cached_control_output_mapping_mismatch")
    if cohort_contract(frame) != config["expected_all_cohort"] or cohort_contract(cohort) != config["expected_test_cohort"]:
        raise ValueError("cohort_digest_mismatch")
    per_run = {str(run): cohort_contract(part) for run, part in cohort.groupby("run_name", sort=False)}
    if per_run != config["expected_test_cohort_per_run"]:
        raise ValueError("per_run_cohort_digest_mismatch")
    return frame, domains, control


def build_fit_plan(frame: pd.DataFrame, protocol: dict[str, Any]) -> list[dict[str, Any]]:
    plans = []
    for index, run in enumerate(protocol["run_order"]):
        if index < protocol["minimum_training_runs"]:
            continue
        current = frame[frame.run_name.eq(run)]
        cutoff = current.decision_ts.min() if not current.empty else None
        train = base.causal_training_rows(frame, protocol["run_order"][:index], current)
        train = train[train.quality_label_observed].copy()
        known_runs = int(train.run_name.nunique())
        eligible = not current.empty and len(train) >= protocol["minimum_training_labels"] and known_runs >= protocol["minimum_training_runs"]
        status = ("fit_with_prior_available_quality_labels" if eligible else "empty_evaluation_cohort" if current.empty
            else "insufficient_prior_quality_runs" if known_runs < protocol["minimum_training_runs"] else "insufficient_prior_quality_labels")
        reference = {"run_name": run, "training_rows": len(train), "training_known_runs": known_runs,
            "evaluation_cohort_rows": len(current), "forecast_rows": len(current) if eligible else 0,
            "decision_cutoff_ts": cutoff.isoformat() if cutoff is not None else None,
            "training_label_max_ts": train.label_available_ts.max().isoformat() if not train.empty else None,
            "training_market_ids_sha256": parent.ids_sha256(train.market_id), "status": status,
            "capacity_constant_log_baseline": float(np.quantile(train.capacity_log_target.to_numpy(dtype=float), .1, method="inverted_cdf")) if eligible else None}
        plans.append({"reference": reference, "train_index": train.index.to_numpy(), "test_index": current.index.to_numpy(), "eligible": eligible})
    return plans


def model_features(frame: pd.DataFrame, kind: str) -> np.ndarray:
    values = histogram.finite_array(frame[FEATURES], kind + "_features")
    if kind == "capacity":
        values = np.column_stack([values, parent.side_sign(frame)])
    elif kind != "price":
        raise ValueError("unknown_model_kind")
    return values


# Numeric histogram serialization is local to this consumer so the frozen
# single-estimator helper remains byte-for-byte unchanged.
def serialize_histogram_model(model: Any, scaler: Any, config: dict[str, Any], kind: str) -> dict[str, Any]:
    contract = config["model_artifact_contract"]
    features = FEATURES if kind == "price" else CAPACITY_FEATURES
    params = model.get_params(deep=False)
    if params != config[kind + "_model_parameters"]:
        raise ValueError("effective_histogram_parameters_mismatch")
    mean, scale = histogram.finite_array(scaler.mean_, "scaler_mean"), histogram.finite_array(scaler.scale_, "scaler_scale")
    if mean.shape != (len(features),) or scale.shape != mean.shape or not (scale > 0).all():
        raise ValueError("invalid_scaler_shape_or_scale")
    baseline = histogram.finite_array(model._baseline_prediction, "baseline_prediction")
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
            if list(predictor.nodes.dtype.names) != histogram.NODE_FIELDS:
                raise ValueError("unexpected_native_node_dtype")
            if np.asarray(predictor.binned_left_cat_bitsets).size or np.asarray(predictor.raw_left_cat_bitsets).size:
                raise ValueError("categorical_bitsets_not_allowed")
            tree = {"iteration": iteration, "tree_index": tree_index,
                "node_dtype": [{"name": name, "dtype": predictor.nodes.dtype.fields[name][0].str} for name in histogram.NODE_FIELDS],
                "nodes": [{field: histogram.encode_node_value(node[field], field) for field in histogram.NODE_FIELDS} for node in predictor.nodes]}
            histogram.validate_serialized_tree(tree, len(features), contract)
            trees.append(tree)
    if len(model._predictors) != iterations or len(trees) != contract["expected_total_trees_per_fit"]:
        raise ValueError("fixed_total_tree_count_mismatch")
    mapper = model._bin_mapper
    thresholds = [histogram.finite_array(values, "bin_thresholds").tolist() for values in mapper.bin_thresholds_]
    counts = np.asarray(mapper.n_bins_non_missing_)
    categorical = np.asarray(mapper.is_categorical_)
    if len(thresholds) != len(features) or counts.shape != (len(features),) or categorical.shape != counts.shape or categorical.any():
        raise ValueError("invalid_numeric_bin_mapper")
    for values, count in zip(thresholds, counts):
        if len(values) + 1 != int(count) or not np.all(np.diff(values) > 0):
            raise ValueError("invalid_bin_threshold_order_or_count")
    if mapper.known_categories is not None:
        raise ValueError("known_categories_not_allowed")
    samples = histogram.finite_array(scaler.n_samples_seen_, "scaler_sample_count")
    if samples.size != 1 or float(samples.item()) <= 0 or float(samples.item()) != int(samples.item()):
        raise ValueError("invalid_scaler_sample_count")
    return {"format": contract["format"], "scaler_mean": mean.tolist(), "scaler_scale": scale.tolist(),
        "scaler_n_samples_seen": int(samples.item()),
        "output_map": ("physical_residual_equals_side_sign_times_raw_output" if kind == "price" else "capacity_equals_maximum_zero_expm1_raw_output"),
        "model_kind": kind,
        "histogram_model": {"estimator": "sklearn.ensemble.HistGradientBoostingRegressor", "effective_parameters": params,
            "features": features.copy(), "baseline_prediction": float(baseline.item()), "n_iter": iterations,
            "n_trees_per_iteration": per_iteration, "trees": trees,
            "bin_mapper": {"bin_thresholds": thresholds, "n_bins_non_missing": counts.astype(int).tolist(),
                "missing_values_bin_idx": int(mapper.missing_values_bin_idx_), "is_categorical": categorical.astype(int).tolist(),
                "subsample": mapper.subsample, "n_bins": mapper.n_bins, "known_categories": None},
            "implementation_modules": implementation_module_hashes()}}


def reconstruct_serialized_histogram(artifact: dict[str, Any], X: Any) -> np.ndarray:
    """Traverse saved numeric nodes on original feature values, without sklearn."""
    features = artifact["histogram_model"]["features"]
    if features not in (FEATURES, CAPACITY_FEATURES):
        raise ValueError("saved_feature_order_mismatch")
    values = histogram.finite_array(X, "reconstruction_inputs")
    if values.ndim != 2 or values.shape[1] != len(features):
        raise ValueError("invalid_reconstruction_feature_shape")
    mean = histogram.finite_array(artifact["scaler_mean"], "saved_scaler_mean")
    scale = histogram.finite_array(artifact["scaler_scale"], "saved_scaler_scale")
    if mean.shape != (len(features),) or scale.shape != mean.shape or not (scale > 0).all():
        raise ValueError("invalid_saved_scaler")
    values = (values - mean) / scale
    if not np.isfinite(values).all():
        raise ValueError("nonfinite_scaled_reconstruction_inputs")
    saved = artifact["histogram_model"]
    if saved["features"] != features:
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
                    predictions[row_index] += histogram.decode_node_value(node["value"], "value")
                    break
                threshold = histogram.decode_node_value(node["num_threshold"], "num_threshold")
                node_index = node["left"] if row[node["feature_idx"]] <= threshold else node["right"]
    if not np.isfinite(predictions).all():
        raise ValueError("nonfinite_reconstructed_prediction")
    return predictions


def fit_predict_once(train: pd.DataFrame, current: pd.DataFrame, config: dict[str, Any], kind: str) -> tuple[np.ndarray, dict[str, Any]]:
    fit_calls = predict_calls = 0
    try:
        if kind not in ("price", "capacity"):
            raise ValueError("unknown_model_kind")
        environment = assert_runtime_environment(config)
        train_X, test_X = model_features(train, kind), model_features(current, kind)
        target = (parent.side_sign(train) * histogram.finite_array(train.bid_residual_target, "price_target")
                  if kind == "price" else histogram.finite_array(train.capacity_log_target, "capacity_target"))
        params = config[kind + "_model_parameters"]
        if params != (PRICE_PARAMETERS if kind == "price" else CAPACITY_PARAMETERS):
            raise ValueError("fixed_histogram_parameters_required")
        model = HistGradientBoostingRegressor(**params)
        if model.get_params(deep=False) != params:
            raise ValueError("effective_histogram_parameters_mismatch")
        scaler = StandardScaler()
        transformed = scaler.fit_transform(train_X)
        fit_calls = 1
        model.fit(transformed, target)
        transformed_test = scaler.transform(test_X)
        predict_calls = 1
        raw = histogram.finite_array(model.predict(transformed_test), kind + "_prediction")
        if raw.shape != (len(current),):
            raise ValueError("invalid_prediction_shape")
        artifact = serialize_histogram_model(model, scaler, config, kind)
        artifact["runtime_environment"] = environment
        reconstructed = reconstruct_serialized_histogram(artifact, test_X)
        if not np.allclose(reconstructed, raw, rtol=1e-12, atol=1e-12):
            raise ValueError("saved_tree_prediction_reconstruction_mismatch")
        artifact["prediction_reconstruction"] = {"rows": len(current), "rtol": 1e-12, "atol": 1e-12,
            "maximum_absolute_difference": float(np.max(np.abs(reconstructed - raw))) if len(current) else 0.}
        canonical(artifact)
        return raw, artifact
    except Exception as exc:
        exc.audit_fit_calls = fit_calls
        exc.audit_predict_calls = predict_calls
        raise


def apply_model_outputs(current: pd.DataFrame, price: np.ndarray, capacity_log: np.ndarray, constant: float) -> pd.DataFrame:
    if price.shape != (len(current),) or capacity_log.shape != price.shape:
        raise ValueError("invalid_prediction_shape")
    if not np.isfinite(price).all() or not np.isfinite(capacity_log).all():
        raise ValueError("nonfinite_prediction")
    with np.errstate(over="ignore", invalid="ignore"):
        capacity = np.maximum(0., np.expm1(capacity_log))
    if not np.isfinite(capacity).all():
        raise ValueError("nonfinite_capacity_output_transform")
    out = current.copy()
    out["model_status"] = "fitted"
    out["predicted_oriented_residual_raw"] = price
    out["predicted_bid_residual_raw"] = parent.side_sign(current) * price
    out["predicted_exit_bid_unclipped"] = out.decision_bid_price + out.predicted_bid_residual_raw
    out["predicted_exit_bid_price"] = out.predicted_exit_bid_unclipped.clip(0, 1)
    out["predicted_capacity_log_raw"] = capacity_log
    out["predicted_exit_capacity_shares"] = capacity
    out["capacity_constant_log_baseline"] = constant
    return out


def paired_price_metrics(scored: pd.DataFrame, control: pd.DataFrame) -> dict[str, Any]:
    joined = scored.merge(control[["market_id", "predicted_exit_bid_price"]], on="market_id", how="left",
        validate="one_to_one", suffixes=("", "_control"), sort=False)
    def metric(part: pd.DataFrame) -> dict[str, Any]:
        known = part.future_bid_price.notna()
        forecast = part.predicted_exit_bid_price.notna() & part.predicted_exit_bid_price_control.notna()
        selected = known & forecast
        a = part.loc[selected, "predicted_exit_bid_price"].to_numpy(dtype=float)
        c = part.loc[selected, "predicted_exit_bid_price_control"].to_numpy(dtype=float)
        y = part.loc[selected, "future_bid_price"].to_numpy(dtype=float)
        p = part.loc[selected, "decision_bid_price"].to_numpy(dtype=float)
        return {"opportunity_rows": len(part), "original_known_targets": int(known.sum()),
            "unknown_targets_retained": int((~known).sum()), "paired_known_forecast_rows": int(selected.sum()),
            "known_targets_excluded_no_forecast": int((known & ~forecast).sum()),
            "A_squared_error_sum": float(np.square(a-y).sum()) if len(y) else None,
            "C_squared_error_sum": float(np.square(c-y).sum()) if len(y) else None,
            "persistence_squared_error_sum": float(np.square(p-y).sum()) if len(y) else None,
            "A_mse": float(np.square(a-y).mean()) if len(y) else None,
            "C_mse": float(np.square(c-y).mean()) if len(y) else None,
            "persistence_mse": float(np.square(p-y).mean()) if len(y) else None}
    return {"aggregate": metric(joined),
        "per_run": [{"run_name": str(run), **metric(part)} for run, part in joined.groupby("run_name", sort=False)],
        "per_side": {str(side): metric(part) for side, part in joined.groupby("side", sort=True)},
        "quality_diagnostics": {str(bool(q)): metric(part) for q, part in joined.groupby("quality_label_observed", sort=True)},
        "basis": "identical_original_known_bid_targets_and_paired_finite_forecasts_not_profit_or_untouched_holdout"}


def capacity_metrics(scored: pd.DataFrame, protocol: dict[str, Any]) -> dict[str, Any]:
    tau = .1
    def metric(part: pd.DataFrame) -> dict[str, Any]:
        known = part.quality_label_observed
        forecast = part.predicted_capacity_log_raw.notna()
        observed = part.loc[known & forecast]
        y = observed.capacity_log_target.to_numpy(dtype=float)
        prediction = observed.predicted_capacity_log_raw.to_numpy(dtype=float)
        constant = observed.capacity_constant_log_baseline.to_numpy(dtype=float)
        def loss(forecast_values):
            errors = y - forecast_values
            return np.maximum(tau * errors, (tau - 1) * errors)
        model_loss, constant_loss = loss(prediction), loss(constant)
        actual = observed.future_effective_exit_capacity.to_numpy(dtype=float)
        cap = observed.predicted_exit_capacity_shares.to_numpy(dtype=float)
        return {"opportunity_rows": len(part), "quality_targets": int(known.sum()),
            "unknown_quality_targets_retained": int((~known).sum()), "paired_quality_forecast_rows": len(observed),
            "quality_targets_excluded_no_forecast": int((known & ~forecast).sum()),
            "model_pinball_sum": float(model_loss.sum()) if len(y) else None,
            "constant_pinball_sum": float(constant_loss.sum()) if len(y) else None,
            "model_pinball_mean": float(model_loss.mean()) if len(y) else None,
            "constant_pinball_mean": float(constant_loss.mean()) if len(y) else None,
            "actual_capacity_below_forecast_count": int(np.sum(actual < cap)),
            "actual_capacity_below_forecast_rate": float(np.mean(actual < cap)) if len(y) else None,
            "actual_capacity_plus_tolerance_below_forecast_count": int(np.sum(actual + 1e-9 < cap)),
            "actual_capacity_plus_tolerance_below_forecast_rate": float(np.mean(actual + 1e-9 < cap)) if len(y) else None,
            "observed_zero_capacity_targets": int(np.sum(y == 0))}
    return {"quantile": tau, "diagnostic_capacity_tolerance_shares": 1e-9,
        "primary_capacity_comparison": "raw_observed_effective_capacity_strictly_less_than_forecast",
        "aggregate": metric(scored),
        "per_run": [{"run_name": str(run), **metric(part)} for run, part in scored.groupby("run_name", sort=False)],
        "per_side": {str(side): metric(part) for side, part in scored.groupby("side", sort=True)},
        "basis": "conditional_on_observed_quality_domain_not_fill_probability_or_guaranteed_coverage"}


def economic_diagnostics(results: list[dict[str, Any]], scored: pd.DataFrame, protocol: dict[str, Any]) -> list[dict[str, Any]]:
    lookup = scored.set_index("market_id")
    summaries = []
    for result in results:
        cash_path = [float(protocol["initial_cash"])]
        entry_quality_known = cap_under_forecast = actual_under_held = unknown_quality = 0
        cap_under_forecast_tol = actual_under_held_tol = 0
        selected_depth_known = selected_depth_under_held = selected_depth_under_held_tol = 0
        for event in result["chain"]:
            if event["status"] == "timed_quote_exit":
                cash_path.append(float(event["cash_after_entry"] + event["payout"]))
            row = lookup.loc[event["market_id"]]
            if row.quality_label_observed:
                entry_quality_known += 1
                actual = float(row.future_effective_exit_capacity)
                forecast = row.get("predicted_exit_capacity_shares")
                if forecast is not None and pd.notna(forecast):
                    cap_under_forecast += int(actual < float(forecast))
                    cap_under_forecast_tol += int(actual + 1e-9 < float(forecast))
                actual_under_held += int(actual < event["shares"])
                actual_under_held_tol += int(actual + 1e-9 < event["shares"])
            else:
                unknown_quality += 1
            depth = event.get("exit_quote_evidence", {}).get("effective_available_shares")
            if depth is not None and math.isfinite(depth) and depth >= 0:
                selected_depth_known += 1
                selected_depth_under_held += int(depth < event["shares"])
                selected_depth_under_held_tol += int(depth + 1e-9 < event["shares"])
        peak, drawdown_abs, drawdown_fraction = cash_path[0], 0., 0.
        for value in cash_path:
            peak = max(peak, value)
            drawdown_abs = max(drawdown_abs, peak - value)
            drawdown_fraction = max(drawdown_fraction, (peak - value) / peak)
        attempts = {}
        for attempt in result["attempts"]:
            attempts[attempt["status"]] = attempts.get(attempt["status"], 0) + 1
        summaries.append({"scope": result["scope"], "block_index": result["block_index"],
            "resolved_cash_cycle_path": cash_path, "maximum_resolved_cycle_drawdown_usd": drawdown_abs,
            "maximum_resolved_cycle_drawdown_fraction": drawdown_fraction,
            "drawdown_basis": "completed_cash_cycles_only_unresolved_inventory_not_marked_to_zero",
            "attempt_status_counts": attempts, "entered_positions": len(result["chain"]),
            "quality_observed_entered_positions": entry_quality_known, "unknown_quality_entered_positions": unknown_quality,
            "quality_observed_actual_capacity_below_forecast_count": cap_under_forecast,
            "quality_observed_actual_capacity_plus_tolerance_below_forecast_count": cap_under_forecast_tol,
            "quality_observed_actual_capacity_below_held_quantity_count": actual_under_held,
            "quality_observed_actual_capacity_plus_tolerance_below_held_quantity_count": actual_under_held_tol,
            "selected_side_exit_depth_observed_entries": selected_depth_known,
            "selected_side_exit_depth_below_held_quantity_count": selected_depth_under_held,
            "selected_side_exit_depth_plus_tolerance_below_held_quantity_count": selected_depth_under_held_tol,
            "diagnostic_capacity_tolerance_shares": 1e-9,
            "ending_liquid_cash": result["ending_cash"], "locked_shares": result["locked_shares"],
            "minimum_cash_stop": result.get("failure_reason") == "cash_cannot_fit_minimum_order"})
    return summaries


def first_economic_difference(reference: list[dict[str, Any]], treatment: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons = []
    for prior, current in zip(reference, treatment):
        row = {"scope": current["scope"], "block_index": current["block_index"]}
        for kind in ("attempts", "chain"):
            difference = None
            left, right = prior[kind], current[kind]
            for index in range(max(len(left), len(right))):
                a = left[index] if index < len(left) else None
                b = right[index] if index < len(right) else None
                def economics(value):
                    if value is None:
                        return None
                    ignored = {"quantity_cap_evidence", "entry_cost_limit_evidence", "predicted_exit_bid_price",
                        "predicted_exit_fill_price", "predicted_exit_net_per_share", "maximum_continuous_price"}
                    return {key: item for key, item in value.items() if key not in ignored}
                if parent.normalized_result(economics(a)) != parent.normalized_result(economics(b)):
                    difference = {"sequence_index": index, "reference": a, "treatment": b}
                    break
            row["first_different_" + kind] = difference
        comparisons.append(row)
    return comparisons


def evaluate(dataset_root: Path, control_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    # Imported late so model helpers remain usable independently of cash replay.
    import cached_quote_execution as execution
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    report = {"format": "offline-quality-capacity-audit-v1", "evaluation_status": "comparison_invalid",
        "strategy_evaluated": False, "comparison_valid": False, "control_execution_reproduced": False,
        "control_model_fit_calls": 0, "control_model_predict_calls": 0,
        "actual_model_fit_calls": 0, "actual_model_predict_calls": 0,
        "price_model_fit_calls": 0, "capacity_model_fit_calls": 0, "price_model_predict_calls": 0,
        "capacity_model_predict_calls": 0, "model_scored_rows": 0,
        "fit_records": [], "pre_fit_gates": [], "arms": {}, "replayed_cells": 0,
        "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False}
    stage = "configuration"
    try:
        validate_config(config)
        protocol = config["base_protocol"]
        report.update(protocol=config, protocol_sha256=digest(config), evaluation_class=config["evaluation_class"])
        stage = "source_and_code_bindings"
        report.update(verified_inputs(config))
        report["pre_fit_gates"].append({"gate": stage, "passed": True})
        stage = "cached_input_cohort_and_quality_contract"
        frame, domains, control = load_inputs(dataset_root, control_root, config)
        tests = protocol["run_order"][protocol["minimum_training_runs"]:]
        cohort = frame[frame.run_name.isin(tests)].copy()
        report["pre_fit_gates"].append({"gate": stage, "passed": True, "all_rows": len(frame), "test_rows": len(cohort)})
        stage = "full_cached_control_candidate_regeneration"
        source = execution.CachedQuoteSource(domains.replace("", None), protocol)
        control_candidates, control_decisions = execution.regenerate_control(cohort, control, config)
        base.storage._private_csv(output / "control-decisions.csv", control_decisions)
        base.storage._private_csv(output / "control-candidates.csv", control_candidates)
        report["pre_fit_gates"].append({"gate": stage, "passed": True, "candidate_rows": len(control_candidates)})
        stage = "full_cached_control_execution"
        controls = execution.replay_arm(control_candidates, source, protocol)
        report["replayed_cells"] = len(controls)
        report["arms"]["C"] = {"candidate_rows": len(control_candidates), "results": controls,
            "prediction_basis": "complete_frozen_prediction_cache_without_fit_or_predict"}
        parent.gate(report, "control_result_count", len(controls), config["expected_control_result_count"])
        parent.gate(report, "full_control_result_fingerprints", [result_fingerprint(cell) for cell in controls],
            config["expected_control_result_fingerprints"])
        report["control_execution_reproduced"] = True
        stage = "runtime_environment"
        report["runtime_environment"] = assert_runtime_environment(config)
        report["implementation_modules"] = implementation_module_hashes()
        stage = "shared_causal_quality_training_plan"
        plans = build_fit_plan(frame, protocol)
        parts = []
        member_path = output / "training-members.csv"
        for plan in plans:
            reference = plan["reference"]
            record = {**reference, "actual_model_fit_calls": 0, "actual_model_predict_calls": 0,
                      "price_model_fit_calls": 0, "capacity_model_fit_calls": 0,
                      "price_model_predict_calls": 0, "capacity_model_predict_calls": 0}
            report["fit_records"].append(record)
            current = frame.loc[plan["test_index"]].copy()
            current["fit_run"] = reference["run_name"]
            if not plan["eligible"]:
                current["model_status"] = reference["status"]
                for name in PREDICTION_FIELDS[2:]:
                    current[name] = np.nan
                parts.append(current)
                base.storage._private_csv(output / "scores.csv", current, append=True)
                continue
            train = frame.loc[plan["train_index"]].copy()
            member = train[["market_id", "run_name", "label_available_ts", "bid_residual_target", "capacity_log_target"]].copy()
            member.insert(0, "fit_run", reference["run_name"])
            member["decision_cutoff_ts"] = pd.Timestamp(reference["decision_cutoff_ts"])
            base.storage._private_csv(member_path, member[MEMBER_FIELDS], append=True)
            outputs = {}
            for kind in ("price", "capacity"):
                stage = kind + "_model_fitting"
                if (report[kind + "_model_fit_calls"] >= config["max_" + kind + "_fit_calls"]
                        or report["actual_model_fit_calls"] >= config["max_new_model_fit_calls"]
                        or report["actual_model_predict_calls"] >= config["max_new_model_predict_calls"]):
                    raise ValueError("model_computation_budget_exceeded")
                try:
                    values, artifact = fit_predict_once(train, current, config, kind)
                except Exception as exc:
                    fc, pc = int(getattr(exc, "audit_fit_calls", 0)), int(getattr(exc, "audit_predict_calls", 0))
                    report["actual_model_fit_calls"] += fc
                    report["actual_model_predict_calls"] += pc
                    report[kind + "_model_fit_calls"] += fc
                    report[kind + "_model_predict_calls"] += pc
                    record["actual_model_fit_calls"] += fc
                    record["actual_model_predict_calls"] += pc
                    record[kind + "_model_fit_calls"] += fc
                    record[kind + "_model_predict_calls"] += pc
                    record["status"] = "model_call_failed"
                    raise
                report["actual_model_fit_calls"] += 1
                report["actual_model_predict_calls"] += 1
                report[kind + "_model_fit_calls"] += 1
                report[kind + "_model_predict_calls"] += 1
                record["actual_model_fit_calls"] += 1
                record["actual_model_predict_calls"] += 1
                record[kind + "_model_fit_calls"] += 1
                record[kind + "_model_predict_calls"] += 1
                outputs[kind] = values
                append_private_jsonl(output / (kind + "-models.jsonl"), {"fit_run": reference["run_name"],
                    "cutoff_ts": reference["decision_cutoff_ts"], "training_rows": len(train),
                    "training_known_runs": reference["training_known_runs"],
                    "training_market_ids_sha256": reference["training_market_ids_sha256"], **artifact})
            current = apply_model_outputs(current, outputs["price"], outputs["capacity"], reference["capacity_constant_log_baseline"])
            parts.append(current)
            report["model_scored_rows"] += len(current)
            base.storage._private_csv(output / "scores.csv", current, append=True)
        if not member_path.exists():
            base.storage._private_csv(member_path, pd.DataFrame(columns=MEMBER_FIELDS))
        scored = pd.concat(parts, ignore_index=True) if parts else cohort.iloc[:0].copy()
        stage = "full_treatment_cohort_preserved"
        parent.gate(report, "full_test_cohort_including_nullable_forecasts", cohort_contract(scored), config["expected_test_cohort"])
        if list(scored.market_id) != list(cohort.market_id):
            raise ValueError("treatment_output_order_mismatch")
        available = scored[scored.model_status.eq("fitted")].copy()
        candidates, decisions = base.decision_cost_candidates(scored, protocol)
        base.storage._private_csv(output / "quality-price-decisions.csv", decisions)
        base.storage._private_csv(output / "quality-price-candidates.csv", candidates)
        caps = dict(zip(available.market_id, available.predicted_exit_capacity_shares))
        stage = "new_treatment_execution"
        a_results = execution.replay_arm(candidates, source, protocol)
        report["replayed_cells"] += len(a_results)
        report["arms"]["A"] = {"evaluation_cohort_rows": len(scored), "model_forecast_rows": len(available),
            "candidate_rows": len(candidates), "results": a_results, "prediction_basis": "quality_training_price_only"}
        b_results = execution.replay_arm(candidates, source, protocol, capacity_cap_by_id=caps)
        report["replayed_cells"] += len(b_results)
        report["arms"]["B"] = {"evaluation_cohort_rows": len(scored), "model_forecast_rows": len(available),
            "candidate_rows": len(candidates), "results": b_results, "prediction_basis": "same_A_price_plus_decision_frozen_capacity_cap"}
        if report["replayed_cells"] != config["max_replay_cells"]:
            raise ValueError("complete_replay_cell_budget_mismatch")
        stage = "paired_prediction_and_cash_metrics"
        report["paired_price_metrics"] = paired_price_metrics(scored, control)
        report["capacity_metrics"] = capacity_metrics(scored, protocol)
        report["economic_diagnostics"] = {"C": economic_diagnostics(controls, scored, protocol),
            "A": economic_diagnostics(a_results, scored, protocol), "B": economic_diagnostics(b_results, scored, protocol)}
        report["first_economic_differences"] = {"A_vs_C": first_economic_difference(controls, a_results),
            "B_vs_A": first_economic_difference(a_results, b_results)}
        report["cash_turnover"] = {"C": parent.turnover_summary(controls, protocol["initial_cash"]),
            "A": parent.turnover_summary(a_results, protocol["initial_cash"]),
            "B": parent.turnover_summary(b_results, protocol["initial_cash"])}
        has_forecasts = bool(report["control_execution_reproduced"] and report["model_scored_rows"] > 0
            and report["actual_model_fit_calls"] > 0 and report["actual_model_predict_calls"] > 0)
        report.update(evaluation_status="evaluated" if has_forecasts else "no_model_forecasts",
            strategy_evaluated=has_forecasts, comparison_valid=True,
            run_order=protocol["run_order"], test_runs=tests,
            evaluation_blocks=base.disjoint_blocks(tests, protocol["evaluation_block_runs"]))
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
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.config.stat().st_size > 262144:
        raise ValueError("private_input_budget_exceeded")
    report = evaluate(args.dataset_root, args.control_root, json.loads(args.config.read_text()), args.output)
    return 0 if report["strategy_evaluated"] and report["comparison_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
