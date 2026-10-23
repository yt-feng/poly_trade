import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from analysis import offline_quality_capacity_audit as audit
from analysis import cached_quote_execution as execution

spec = importlib.util.spec_from_file_location("synthetic_quality_parent_fixture", Path(__file__).with_name("test_offline_side_interaction_audit.py"))
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


def fixture(root):
    old, frame, _ = fixture_module.fixture()
    protocol = old["base_protocol"]
    frame["first_quote_ts"] = frame.decision_ts - pd.Timedelta(seconds=120)
    frame["target_entry_quote_ts"] = frame.decision_ts + pd.Timedelta(seconds=12)
    frame["target_exit_arrival_ts"] = frame.decision_ts + pd.Timedelta(seconds=50)
    books = {}
    for row in frame.itertuples():
        entry = {"ts_utc": row.target_entry_quote_ts}
        exit_ = {"ts_utc": row.target_exit_arrival_ts}
        for side in ("up", "down"):
            entry.update({f"buy_{side}_cents": 42., f"sell_{side}_cents": 40., f"buy_{side}_size": 1000., f"sell_{side}_size": 1000.})
            exit_.update({f"buy_{side}_cents": 47., f"sell_{side}_cents": 45., f"buy_{side}_size": 1000., f"sell_{side}_size": 1000.})
        books[row.market_id] = pd.DataFrame([entry, exit_] if pd.notna(row.future_bid_price) else [entry])
    domains = pd.DataFrame([audit.domains_export.export_quote_row(row, books[row.market_id], protocol) for _, row in frame.iterrows()], columns=audit.domains_export.COLUMNS)
    cohort = frame[frame.run_name.eq("current")].copy()
    control = cohort[["market_id", "run_name"]].copy()
    control["fit_run"] = control.run_name
    control["predicted_oriented_residual_raw"] = audit.parent.side_sign(cohort) * .4
    control["predicted_bid_residual_raw"] = .4
    control["predicted_exit_bid_unclipped"] = .8
    control["predicted_exit_bid_price"] = .8
    control = control[audit.CONTROL_COLUMNS]
    control_scores = cohort.copy()
    for name in audit.CONTROL_COLUMNS[3:]:
        control_scores[name] = control[name].to_numpy()
    candidates, _ = audit.base.decision_cost_candidates(control_scores, protocol)
    frozen = [{key: str(row[key]) for key in audit.parent.CANDIDATE_FIELDS} for _, row in candidates.iterrows()]
    source = execution.CachedQuoteSource(domains, protocol)
    results = execution.replay_arm(candidates, source, protocol)
    dataset, controls = root / "dataset", root / "control"
    dataset.mkdir(); controls.mkdir()
    audit.base.storage._private_csv(dataset / "features.csv", frame)
    audit.base.storage._private_csv(dataset / "quote-cash-domains.csv", domains)
    audit.base.storage._private_csv(controls / "control-predictions.csv", control)
    config = {"evaluation_mode": "cached_quality_capacity", "expected_code_commit": "c"*40,
        "source_commit": protocol["source_commit"], "base_protocol": protocol,
        "frozen_base_protocol_sha256": audit.digest(protocol),
        "parent_bindings": {"unchanged_code_file_sha256": {name: audit.file_sha256(audit.CURRENT_DIR.parent / name) for name in audit.UNCHANGED_FILES}},
        "cache_bindings": {"features_csv_sha256": audit.file_sha256(dataset / "features.csv"),
            "domains_csv_sha256": audit.file_sha256(dataset / "quote-cash-domains.csv"),
            "control_predictions_csv_sha256": audit.file_sha256(controls / "control-predictions.csv")},
        "price_model_parameters": copy.deepcopy(audit.PRICE_PARAMETERS), "capacity_model_parameters": copy.deepcopy(audit.CAPACITY_PARAMETERS),
        "model_artifact_contract": copy.deepcopy(audit.MODEL_ARTIFACT_CONTRACT), "model_contract": copy.deepcopy(audit.MODEL_CONTRACT),
        "control_model_refit_allowed": False, "control_model_predict_allowed": False, "live_orders_enabled": False,
        "new_data_collection_allowed": False, "actual_fills": 0, "verified_canary_fills": 0,
        "max_price_fit_calls": 1, "max_capacity_fit_calls": 1, "max_new_model_fit_calls": 2, "max_new_model_predict_calls": 2,
        "max_replay_cells": 6, "max_new_cloud_runs": 1, "expected_control_result_count": 2,
        "max_private_config_bytes": 262144, "expected_all_cohort": audit.cohort_contract(frame),
        "expected_test_cohort": audit.cohort_contract(cohort), "expected_test_cohort_per_run": {"current": audit.cohort_contract(cohort)},
        "control_result_fingerprint_version": audit.parent.FINGERPRINT_VERSION,
        "expected_control_result_fingerprints": [audit.result_fingerprint(row) for row in results],
        "frozen_control_candidate_fields": audit.parent.CANDIDATE_FIELDS.copy(), "frozen_control_candidates": frozen,
        "frozen_control_candidate_rows_sha256": audit.digest(frozen), "expected_control_candidate_count": len(frozen),
        "environment": {"python_major_minor": "3.11", "packages": {name: "1.2.3" for name in audit.histogram.PACKAGE_MODULES},
            "must_assert_effective_get_params_exact_before_fit": True, "must_assert_runtime_versions_before_model_construction": True,
            "must_record_loaded_histogram_implementation_module_file_hashes": True}, "evaluation_class": "synthetic_only"}
    return config, dataset, controls, frame, domains


def fake_fit(train, current, config, kind):
    raw = audit.parent.side_sign(current) * .2 if kind == "price" else np.full(len(current), np.log1p(20.))
    return raw, {"model_kind": kind, "synthetic_stub": True}


class QualityCapacityAuditTests(unittest.TestCase):
    def run_synthetic(self, mutate=None, fit=fake_fit, runtime_failure=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, dataset, controls, frame, domains = fixture(root)
            if mutate:
                mutate(config, dataset, controls, frame, domains)
            with patch.object(audit, "verified_inputs", return_value={"code_commit": config["expected_code_commit"]}), \
                    patch.object(audit, "assert_runtime_environment", return_value={}, side_effect=runtime_failure), \
                    patch.object(audit, "implementation_module_hashes", return_value=[]), \
                    patch.object(audit, "fit_predict_once", side_effect=fit) as fitted:
                report = audit.evaluate(dataset, controls, config, root / "output")
            scores = pd.read_csv(root / "output" / "scores.csv") if (root / "output" / "scores.csv").exists() else None
            members = pd.read_csv(root / "output" / "training-members.csv") if (root / "output" / "training-members.csv").exists() else None
            return report, fitted.call_count, scores, members

    def test_full_control_before_two_models_and_unknown_retained(self):
        report, fits, scores, members = self.run_synthetic()
        self.assertEqual(report["evaluation_status"], "evaluated", report.get("failure"))
        self.assertEqual(fits, 2)
        self.assertEqual(report["actual_model_fit_calls"], 2)
        self.assertEqual(report["control_model_fit_calls"], 0)
        self.assertEqual(report["replayed_cells"], 6)
        self.assertEqual(len(scores), 3)
        self.assertEqual(scores.future_bid_price.isna().sum(), 1)
        self.assertEqual(len(members), 100)
        self.assertEqual(set(members.fit_run), {"current"})
        self.assertEqual(report["paired_price_metrics"]["aggregate"]["paired_known_forecast_rows"], 2)

    def test_control_fingerprint_mutation_fails_before_fit(self):
        report, fits, *_ = self.run_synthetic(lambda c, *_: c["expected_control_result_fingerprints"].__setitem__(0, "0"*64))
        self.assertEqual(fits, 0)
        self.assertEqual(report["failure"]["reason"], "full_control_result_fingerprints_mismatch")

    def test_byte_cohort_model_environment_and_budget_gates(self):
        mutations = [lambda c, *_: c["cache_bindings"].update(features_csv_sha256="0"*64),
            lambda c, *_: c["expected_test_cohort"].update(scored_ids_sha256="0"*64),
            lambda c, *_: c["price_model_parameters"].update(max_depth=4),
            lambda c, *_: c["capacity_model_parameters"].update(quantile=.2),
            lambda c, *_: c.update(max_new_model_fit_calls=3),
            lambda c, *_: c["model_contract"].update(capacity_features=audit.FEATURES)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                report, fits, *_ = self.run_synthetic(mutate)
                self.assertEqual(fits, 0)
                self.assertFalse(report["comparison_valid"])
        report, fits, *_ = self.run_synthetic(runtime_failure=ValueError("version_changed"))
        self.assertEqual(fits, 0)
        self.assertTrue(report["control_execution_reproduced"])

    def test_quality_membership_only_retains_full_test_cohort_and_no_backfill(self):
        def mutate(c, dataset, controls, frame, domains):
            # One incomplete past other-side quote makes fewer than100 Q labels.
            row = 0
            domains.loc[row, "exit_buy_down_size"] = np.nan
            domains.loc[row, "exit_full_book_valid"] = False
            domains.loc[row, "paired_high_quality_quotes"] = False
            domains.loc[row, "simulation_cash_domain_observed"] = False
            for field in audit.domains_export.DERIVED_COLUMNS:
                domains.loc[row, field] = None
            audit.base.storage._private_csv(dataset / "quote-cash-domains.csv", domains)
            c["cache_bindings"]["domains_csv_sha256"] = audit.file_sha256(dataset / "quote-cash-domains.csv")
        report, fits, scores, members = self.run_synthetic(mutate)
        self.assertEqual(report["evaluation_status"], "no_model_forecasts", report.get("failure"))
        self.assertEqual(fits, 0)
        self.assertFalse(report["strategy_evaluated"])
        self.assertEqual(report["evaluation_status"], "no_model_forecasts")
        self.assertEqual(len(scores), 3)
        self.assertTrue(scores.predicted_exit_bid_price.isna().all())
        self.assertEqual(report["paired_price_metrics"]["aggregate"]["known_targets_excluded_no_forecast"], 2)
        self.assertEqual(len(members), 0)

    def test_shared_plan_strict_exit_cutoff_previous_runs_and_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, dataset, control, _, _ = fixture(Path(tmp))
            frame, *_ = audit.load_inputs(dataset, control, c)
            plan = audit.build_fit_plan(frame, c["base_protocol"])[0]
            self.assertEqual(plan["reference"]["training_rows"], 100)
            train_index = plan["train_index"][0]
            cutoff = pd.Timestamp(plan["reference"]["decision_cutoff_ts"])
            frame.loc[train_index, "label_available_ts"] = cutoff
            plan = audit.build_fit_plan(frame, c["base_protocol"])[0]
            self.assertEqual(plan["reference"]["training_rows"], 99)
            self.assertFalse(plan["eligible"])
            self.assertTrue(frame.loc[frame.future_bid_price.isna(), "capacity_log_target"].isna().all())

    def test_observed_zero_capacity_is_training_label_and_invalid_gate_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, dataset, control, _, domains = fixture(Path(tmp))
            side = domains.loc[0, "side"]
            domains.loc[0, f"exit_sell_{side}_size"] = 0.
            domains.loc[0, "exit_available_shares"] = 0.
            domains.loc[0, "exit_effective_available_shares"] = 0.
            domains.loc[0, "maximum_pair_capacity_shares"] = 0.
            domains.loc[0, "minimum_order_has_pair_capacity"] = False
            audit.base.storage._private_csv(dataset / "quote-cash-domains.csv", domains)
            c["cache_bindings"]["domains_csv_sha256"] = audit.file_sha256(dataset / "quote-cash-domains.csv")
            frame, *_ = audit.load_inputs(dataset, control, c)
            self.assertTrue(frame.loc[0, "quality_label_observed"])
            self.assertEqual(frame.loc[0, "capacity_log_target"], 0.)
            self.assertEqual(audit.build_fit_plan(frame, c["base_protocol"])[0]["reference"]["training_rows"], 100)
            domains.loc[0, "simulation_cash_domain_observed"] = False
            audit.base.storage._private_csv(dataset / "quote-cash-domains.csv", domains)
            c["cache_bindings"]["domains_csv_sha256"] = audit.file_sha256(dataset / "quote-cash-domains.csv")
            with self.assertRaisesRegex(ValueError, "simulation_quality_gate_mismatch"):
                audit.load_inputs(dataset, control, c)

    def test_future_quality_not_model_feature_and_capacity_side_is_only_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, d, p, *_ = fixture(Path(tmp))
            frame, *_ = audit.load_inputs(d, p, c)
            values = audit.model_features(frame, "capacity")
            self.assertEqual(values.shape[1], len(audit.FEATURES)+1)
            np.testing.assert_array_equal(values[:, -1], audit.parent.side_sign(frame))
            frame["capacity_log_target"] = 1e99
            frame["quality_label_observed"] = False
            frame["future_bid_price"] = .999
            np.testing.assert_array_equal(values, audit.model_features(frame, "capacity"))

    def test_failed_model_calls_are_counted_and_no_replay_after_failure(self):
        def fail(train, current, config, kind):
            exc = ValueError("synthetic_failed_predict")
            exc.audit_fit_calls = 1
            exc.audit_predict_calls = 1
            raise exc
        report, fits, *_ = self.run_synthetic(fit=fail)
        self.assertEqual(fits, 1)
        self.assertEqual(report["actual_model_fit_calls"], 1)
        self.assertEqual(report["actual_model_predict_calls"], 1)
        self.assertEqual(report["replayed_cells"], 2)
        self.assertFalse(report["strategy_evaluated"])

    def test_output_transform_overflow_fails_instead_of_clipping(self):
        frame = pd.DataFrame({"side": ["up"], "decision_bid_price": [.4]})
        with self.assertRaisesRegex(ValueError, "nonfinite_capacity"):
            audit.apply_model_outputs(frame, np.array([0.]), np.array([1000.]), 0.)
        out = audit.apply_model_outputs(frame, np.array([.2]), np.array([-1.]), 0.)
        self.assertEqual(out.predicted_exit_capacity_shares.iloc[0], 0.)

    def test_direct_runtime_and_effective_parameters_precede_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, d, p, *_ = fixture(Path(tmp))
            frame, *_ = audit.load_inputs(d, p, c)
            with patch.object(audit, "assert_runtime_environment", side_effect=ValueError("runtime_changed")), \
                    patch.object(audit, "HistGradientBoostingRegressor") as model:
                with self.assertRaises(ValueError) as exc:
                    audit.fit_predict_once(frame.iloc[:100], frame.iloc[100:], c, "price")
                self.assertEqual(exc.exception.audit_fit_calls, 0)
                model.assert_not_called()
            bad = Mock(); bad.get_params.return_value = {}
            with patch.object(audit, "assert_runtime_environment", return_value={}), \
                    patch.object(audit, "HistGradientBoostingRegressor", return_value=bad), patch.object(audit, "StandardScaler") as scaler:
                with self.assertRaisesRegex(ValueError, "effective_histogram_parameters"):
                    audit.fit_predict_once(frame.iloc[:100], frame.iloc[100:], c, "capacity")
                scaler.assert_not_called()


    def test_existing_private_input_source_schema_is_repository_not_repo_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, *_ = fixture(Path(tmp))
            config["private_inputs"] = {name: {"descriptor_sha256": "e"*64,
                "source": {"repository": "synthetic/private-archive", "commit": "d"*40,
                    "path": "research_vault/episodes/" + "f"*64 + "/part-0000.vault"}} for name in ("dataset", "control")}
            audit.validate_config(config)
            record = config["private_inputs"]["control"]["source"]
            record["repo"] = record.pop("repository")
            with self.assertRaisesRegex(ValueError, "invalid_private_input_binding"):
                audit.validate_config(config)

    def test_capacity_metrics_use_exact_observed_depth_strict_and_tolerant_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, d, control, *_ = fixture(Path(tmp))
            frame, *_ = audit.load_inputs(d, control, config)
            current = frame[frame.run_name.eq("current")].copy()
            prediction = np.full(len(current), np.log1p(100.))
            scored = audit.apply_model_outputs(current, np.zeros(len(current)), prediction, np.log1p(100.))
            metrics = audit.capacity_metrics(scored, config["base_protocol"])
            self.assertEqual(metrics["aggregate"]["paired_quality_forecast_rows"], 2)
            self.assertEqual(metrics["aggregate"]["actual_capacity_below_forecast_count"], 2)
            self.assertEqual(metrics["aggregate"]["actual_capacity_plus_tolerance_below_forecast_count"], 0)
            self.assertEqual(metrics["aggregate"]["model_pinball_sum"], 0.)
            self.assertEqual(metrics["diagnostic_capacity_tolerance_shares"], 1e-9)
            np.testing.assert_array_equal(scored.loc[scored.quality_label_observed, "future_effective_exit_capacity"], 100.)

    def test_timestamp_text_equivalence_real_difference_and_nanoseconds_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, d, control, *_ = fixture(Path(tmp))
            frame, *_ = audit.load_inputs(d, control, c)
            self.assertEqual(len(frame), 103)  # Space versus ISO T is equivalent.
            domains = pd.read_csv(d / "quote-cash-domains.csv", dtype=str, keep_default_na=False)
            stamp = pd.Timestamp(domains.loc[0, "decision_ts"])
            domains.loc[0, "decision_ts"] = (stamp + pd.Timedelta(nanoseconds=1)).isoformat()
            audit.base.storage._private_csv(d / "quote-cash-domains.csv", domains)
            c["cache_bindings"]["domains_csv_sha256"] = audit.file_sha256(d / "quote-cash-domains.csv")
            with self.assertRaisesRegex(ValueError, "domain_join_mismatch_decision_ts"):
                audit.load_inputs(d, control, c)
            values = audit._times(pd.Series(["2026-01-01T00:00:00.000000001Z"]), "test")
            self.assertEqual(values.iloc[0].nanosecond, 1)

    def test_native_both_model_artifacts_reconstruct_without_predict(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, d, control, *_ = fixture(Path(tmp))
            frame, *_ = audit.load_inputs(d, control, c)
            train, current = frame.iloc[:100], frame.iloc[100:]
            for kind, features in (("price", audit.FEATURES), ("capacity", audit.CAPACITY_FEATURES)):
                with self.subTest(kind=kind), patch.object(audit, "assert_runtime_environment", return_value={}):
                    raw, artifact = audit.fit_predict_once(train, current, c, kind)
                    self.assertEqual(artifact["histogram_model"]["features"], features)
                    self.assertEqual(len(artifact["histogram_model"]["trees"]), 100)
                    with patch.object(audit.HistGradientBoostingRegressor, "predict", side_effect=AssertionError("no estimator prediction")):
                        reconstructed = audit.reconstruct_serialized_histogram(artifact, audit.model_features(current, kind))
                    np.testing.assert_allclose(raw, reconstructed, rtol=1e-12, atol=1e-12)
                    self.assertEqual(artifact["scaler_n_samples_seen"], 100)
                    audit.canonical(artifact)


if __name__ == "__main__":
    unittest.main()
