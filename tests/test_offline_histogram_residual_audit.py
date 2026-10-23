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

from analysis import offline_histogram_residual_audit as audit

spec = importlib.util.spec_from_file_location("synthetic_parent_fixture", Path(__file__).with_name("test_offline_side_interaction_audit.py"))
parent_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parent_fixture)


def fixture(runs=None):
    parent, frame, results = parent_fixture.fixture(runs)
    config = copy.deepcopy(parent)
    config.update(protocol_id="synthetic_histogram", evaluation_mode="paired_histogram_residual",
        expected_code_commit="e" * 40, parent_code_commit=parent["expected_code_commit"],
        parent_protocol=copy.deepcopy(parent), control_model_predict_allowed=False, verified_canary_fills=0)
    config["parent_bindings"]["base_protocol_sha256"] = parent["parent_bindings"]["protocol_sha256"]
    config["parent_bindings"]["protocol_sha256"] = hashlib.sha256(audit.canonical(parent)).hexdigest()
    config["parent_bindings"]["unchanged_code_file_sha256"]["analysis/offline_side_interaction_audit.py"] = audit.file_sha256(audit.CURRENT_DIR / "offline_side_interaction_audit.py")
    config["treatment"] = {key: copy.deepcopy(parent["treatment"][key]) for key in (
        "side_sign", "training_target", "raw_model_output", "physical_residual_prediction", "physical_future_bid_prediction", "scaler")}
    config["treatment"].update(intercept="fit_and_invert_entire_histogram_baseline_and_tree_sum_with_same_side_sign",
        model={"estimator": "sklearn.ensemble.HistGradientBoostingRegressor", "parameters": copy.deepcopy(audit.MODEL_PARAMETERS)})
    config["environment"] = {"packages": {name: "1.2.3" for name in audit.PACKAGE_MODULES}, "python_major_minor": "3.11",
        "must_assert_effective_get_params_exact_before_fit": True,
        "must_assert_runtime_versions_before_model_construction": True,
        "must_record_loaded_histogram_implementation_module_file_hashes": True}
    config["model_artifact_contract"] = copy.deepcopy(audit.MODEL_ARTIFACT_CONTRACT)
    return config, frame, results


def fake_fit(train, current, config):
    out, _ = parent_fixture.fake_fit(train, current, config["base_protocol"])
    return out, {"scaler_mean": [0.] * len(audit.FEATURES), "scaler_scale": [1.] * len(audit.FEATURES),
        "scaler_n_samples_seen": len(train), "histogram_model": {"synthetic_stub": True}, "orientation": {}}


class HistogramResidualAuditTests(unittest.TestCase):
    def run_synthetic(self, config, frame, results, fit_side_effect=fake_fit, source_failure=None,
                      environment_failure=None, books=None, replay_real=False):
        original_replay = audit.replay_arm
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(audit, "verified_inputs", side_effect=source_failure,
                    return_value={"source_commit": config["source_commit"], "code_commit": config["expected_code_commit"]}) as verify, \
                patch.object(audit.base, "prepare_archive", return_value=(frame, books or {}, {"admitted_markets": len(frame)})) as prepare, \
                patch.object(audit, "replay_arm", return_value=results, side_effect=original_replay if replay_real else None) as replay, \
                patch.object(audit, "assert_runtime_environment", return_value={"synthetic": True}, side_effect=environment_failure) as environment, \
                patch.object(audit, "implementation_module_hashes", return_value=[]) , \
                patch.object(audit, "fit_predict_once", side_effect=fit_side_effect) as fit, \
                patch.object(audit.parent_audit, "fit_predict_once", side_effect=AssertionError("no parent fitting")), \
                patch.object(audit.base, "fit_predict_once", side_effect=AssertionError("no ancestor fitting")):
            output = Path(tmp) / "result"
            result = audit.evaluate(Path(tmp) / "never-read-source", config, output)
            saved = json.loads((output / "summary.json").read_text())
            self.assertEqual(saved["evaluation_status"], result["evaluation_status"])
            scores = pd.read_csv(output / "scores.csv") if (output / "scores.csv").exists() else None
            models = [json.loads(line) for line in (output / "fitted-models.jsonl").read_text().splitlines()] if (output / "fitted-models.jsonl").exists() else []
            members = pd.read_csv(output / "training-members.csv") if (output / "training-members.csv").exists() else None
            return result, {"verify": verify.call_count, "prepare": prepare.call_count, "replay": replay.call_count,
                            "fit": fit.call_count, "environment": environment.call_count}, scores, members, models

    def test_full_control_and_cohort_gates_precede_new_fit_unknown_retained(self):
        config, frame, results = fixture()
        report, calls, scores, members, models = self.run_synthetic(config, frame, results)
        self.assertEqual(report["evaluation_status"], "evaluated", report.get("failure"))
        self.assertEqual(calls, {"verify": 1, "prepare": 1, "replay": 2, "fit": 1, "environment": 1})
        self.assertTrue(report["strategy_evaluated"])
        self.assertTrue(report["comparison_valid"])
        self.assertEqual(report["control_model_fit_calls"], 0)
        self.assertEqual(report["control_model_predict_calls"], 0)
        self.assertEqual(report["actual_model_fit_calls"], 1)
        self.assertEqual(report["actual_model_predict_calls"], 1)
        self.assertEqual(len(scores), 3)
        self.assertEqual(scores.future_bid_price.isna().sum(), 1)
        self.assertEqual(list(members), audit.MEMBER_FIELDS)
        self.assertEqual(len(members), 100)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["training_rows"], 100)
        self.assertEqual(report["arms"]["histogram_residual"]["candidate_rows"], 3)
        self.assertIn("paired_per_run", report["predictive_comparison"])

    def test_source_and_six_frozen_file_gate_precedes_archive_and_model(self):
        config, frame, results = fixture()
        report, calls, *_ = self.run_synthetic(config, frame, results, source_failure=ValueError("immutable_parent_helper_bytes_changed"))
        self.assertEqual(report["evaluation_status"], "comparison_invalid")
        self.assertEqual(calls["prepare"], 0)
        self.assertEqual(calls["fit"], 0)
        self.assertEqual(calls["environment"], 0)

    def test_parent_protocol_hash_and_model_parameter_mutations_rejected(self):
        for mutation in (lambda c: c["parent_protocol"].update(protocol_id="changed"),
                         lambda c: c["base_protocol"].update(hold_seconds=31),
                         lambda c: c["treatment"]["model"]["parameters"].update(max_depth=4),
                         lambda c: c["parent_bindings"]["unchanged_code_file_sha256"].pop("analysis/offline_side_interaction_audit.py"),
                         lambda c: c["model_artifact_contract"].update(expected_n_iter_per_fit=99)):
            with self.subTest(mutation=mutation):
                config, frame, results = fixture(); mutation(config)
                report, calls, *_ = self.run_synthetic(config, frame, results)
                self.assertEqual(report["failure"]["stage"], "configuration")
                self.assertEqual(calls["prepare"], 0)
                self.assertEqual(calls["fit"], 0)

    def test_feature_member_cohort_and_persistence_mismatch_prevents_fit(self):
        for mutation in (lambda c: c["parent_bindings"].update(features_csv_sha256="0" * 64),
                         lambda c: c["parent_bindings"].update(training_members_csv_sha256="0" * 64),
                         lambda c: c["cached_parent_prediction_metrics"]["aggregate_known_targets"].update(baseline_mse=.8),
                         lambda c: c["expected_scored_cohort"].update(unknown_ids_sha256="0" * 64)):
            with self.subTest(mutation=mutation):
                config, frame, results = fixture(); mutation(config)
                report, calls, *_ = self.run_synthetic(config, frame, results)
                self.assertEqual(report["evaluation_status"], "comparison_invalid")
                self.assertEqual(calls["replay"], 0)
                self.assertEqual(calls["fit"], 0)
                self.assertEqual(calls["environment"], 0)

    def test_control_attempt_fingerprint_precedes_model_construction(self):
        config, frame, results = fixture()
        results[0]["attempts"] = [{"status": "synthetic_changed_quote"}]
        with patch.object(audit, "HistGradientBoostingRegressor", side_effect=AssertionError("must not construct")) as ctor:
            report, calls, *_ = self.run_synthetic(config, frame, results)
        self.assertEqual(report["failure"]["reason"], "full_control_result_fingerprints_mismatch")
        self.assertEqual(calls["fit"], 0)
        self.assertEqual(calls["environment"], 0)
        self.assertEqual(ctor.call_count, 0)

    def test_actual_synthetic_quote_depth_change_prevents_fit(self):
        config, frame, _ = fixture()
        control = audit.frozen_candidates(config); row = control.iloc[0]
        arrival = row.decision_ts + pd.Timedelta(seconds=12)
        book = pd.DataFrame({"ts_utc": [arrival, arrival + pd.Timedelta(seconds=38)],
            "buy_up_cents": [42., 47.], "buy_up_size": [1000., 1000.],
            "sell_up_cents": [40., 45.], "sell_up_size": [1000., 1000.], "quote_conflict": [False, False]})
        original = audit.replay_arm(control, {row.market_id: book}, config["base_protocol"])
        config["expected_control_result_fingerprints"] = [audit.result_fingerprint(cell) for cell in original]
        changed = book.copy(); changed.loc[1, "sell_up_size"] = .1
        report, calls, *_ = self.run_synthetic(config, frame, original, books={row.market_id: changed}, replay_real=True)
        self.assertEqual(report["failure"]["reason"], "full_control_result_fingerprints_mismatch")
        self.assertEqual(calls["fit"], 0)

    def test_runtime_mismatch_precedes_construction_even_after_valid_control(self):
        config, frame, results = fixture()
        with patch.object(audit, "HistGradientBoostingRegressor", side_effect=AssertionError("must not construct")) as ctor:
            report, calls, *_ = self.run_synthetic(config, frame, results, environment_failure=ValueError("runtime_version_binding_mismatch"))
        self.assertTrue(report["control_execution_reproduced"])
        self.assertEqual(report["evaluation_status"], "evaluation_failed")
        self.assertEqual(report["actual_model_fit_calls"], 0)
        self.assertEqual(calls["fit"], 0)
        self.assertEqual(ctor.call_count, 0)

    def test_direct_fit_environment_check_precedes_constructor(self):
        config, frame, _ = fixture()
        with patch.object(audit, "assert_runtime_environment", side_effect=ValueError("runtime_version_binding_mismatch")), \
                patch.object(audit, "HistGradientBoostingRegressor") as ctor:
            with self.assertRaises(ValueError) as caught:
                audit.fit_predict_once(frame.iloc[:100], frame.iloc[100:], config)
        self.assertEqual(caught.exception.audit_fit_calls, 0)
        self.assertEqual(caught.exception.audit_predict_calls, 0)
        ctor.assert_not_called()

    def test_effective_parameter_mismatch_precedes_scaler_and_fit(self):
        config, frame, _ = fixture()
        model = Mock(); model.get_params.return_value = {**audit.MODEL_PARAMETERS, "max_iter": 99}
        with patch.object(audit, "assert_runtime_environment", return_value={}), \
                patch.object(audit, "HistGradientBoostingRegressor", return_value=model), patch.object(audit, "StandardScaler") as scaler:
            with self.assertRaisesRegex(ValueError, "effective_histogram_parameters") as caught:
                audit.fit_predict_once(frame.iloc[:100], frame.iloc[100:], config)
        self.assertEqual(caught.exception.audit_fit_calls, 0)
        scaler.assert_not_called(); model.fit.assert_not_called()

    def test_serialization_failure_preserves_one_fit_one_predict(self):
        config, frame, _ = fixture()
        model = Mock(); model.get_params.return_value = copy.deepcopy(audit.MODEL_PARAMETERS)
        model.predict.return_value = np.zeros(3)
        with patch.object(audit, "assert_runtime_environment", return_value={}), \
                patch.object(audit, "HistGradientBoostingRegressor", return_value=model), \
                patch.object(audit, "serialize_histogram_model", side_effect=ValueError("synthetic_export_failure")):
            with self.assertRaisesRegex(ValueError, "synthetic_export_failure") as caught:
                audit.fit_predict_once(frame.iloc[:100], frame.iloc[100:], config)
        self.assertEqual(caught.exception.audit_fit_calls, 1)
        self.assertEqual(caught.exception.audit_predict_calls, 1)
        self.assertEqual(model.fit.call_count, 1)
        self.assertEqual(model.predict.call_count, 1)
        np.testing.assert_allclose(model.fit.call_args.args[1], audit.parent_audit.side_sign(frame.iloc[:100]) * .05)

    def test_test_transform_failure_does_not_count_a_prediction_call(self):
        config, frame, _ = fixture()
        model = Mock(); model.get_params.return_value = copy.deepcopy(audit.MODEL_PARAMETERS)
        scaler = Mock(); scaler.fit_transform.return_value = np.zeros((100, len(audit.FEATURES)))
        scaler.transform.side_effect = ValueError("synthetic_test_transform_failure")
        with patch.object(audit, "assert_runtime_environment", return_value={}), \
                patch.object(audit, "HistGradientBoostingRegressor", return_value=model), \
                patch.object(audit, "StandardScaler", return_value=scaler):
            with self.assertRaisesRegex(ValueError, "synthetic_test_transform_failure") as caught:
                audit.fit_predict_once(frame.iloc[:100], frame.iloc[100:], config)
        self.assertEqual(caught.exception.audit_fit_calls, 1)
        self.assertEqual(caught.exception.audit_predict_calls, 0)
        model.fit.assert_called_once(); model.predict.assert_not_called()

    def test_later_failure_keeps_prior_scores_and_model_jsonl(self):
        config, frame, results = fixture(["past0", "past1", "past2", "past3", "current", "next"])
        count = []
        def fail_second(train, current, protocol):
            count.append(len(current))
            if len(count) == 2:
                failure = RuntimeError("synthetic_second_fit_failure")
                failure.audit_fit_calls = 1; failure.audit_predict_calls = 0
                raise failure
            return fake_fit(train, current, protocol)
        report, calls, scores, members, models = self.run_synthetic(config, frame, results, fit_side_effect=fail_second)
        self.assertEqual(report["evaluation_status"], "evaluation_failed")
        self.assertEqual(report["actual_model_fit_calls"], 2)
        self.assertEqual(report["actual_model_predict_calls"], 1)
        self.assertEqual(len(scores), 3)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["fit_run"], "current")
        self.assertEqual(report["model_scored_rows"], 3)
        self.assertIn("fitted-models.jsonl", report["artifacts"])

    def test_append_jsonl_refuses_nonfinite_before_modifying_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.jsonl"
            audit.append_private_jsonl(path, {"saved": 1})
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                audit.append_private_jsonl(path, {"invalid": float("nan")})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
