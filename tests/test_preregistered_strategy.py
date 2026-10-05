import json
from pathlib import Path
import tempfile
import unittest

from analysis.preregistered_strategy import (
    audit_manifest,
    file_sha256,
    main,
    manifest_digest,
    parameter_grid_digest,
    validate_manifest,
)


ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "research" / "strategy" / "experiments" / "EXP-0002-btc5m-preregistered-reference.json"
OBSERVATIONS = ROOT / "tests" / "fixtures" / "v3_observation_valid.jsonl"
LABELS = ROOT / "tests" / "fixtures" / "walk_forward_labels_valid.jsonl"
CODE_COMMIT = "c3ff9549ad0b62ec19fa21c7d917781cd5e4273a"


def registered_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def context(manifest: dict, **overrides):
    values = {
        "phase": "pre_evaluation",
        "observations_sha256": file_sha256(OBSERVATIONS),
        "labels_sha256": file_sha256(LABELS),
        "code_commit": CODE_COMMIT,
        "evaluation_dates_utc": ["2026-10-03"],
        "parameters": {"edge_buffer": "0", "latency_ms": 1000, "order_size": "5", "order_ttl_ms": 5000,
                        "train_duration_ms": 3600000, "test_duration_ms": 900000, "purge_ms": 300000,
                        "embargo_ms": 1000, "min_training_events": 10, "calibration_bins": 10},
    }
    values.update(overrides)
    return audit_manifest(manifest, **values)


class PreRegisteredStrategyTests(unittest.TestCase):
    def test_registered_manifest_has_stable_grid_and_self_digest(self):
        manifest = registered_manifest()
        self.assertEqual(validate_manifest(manifest), [])
        self.assertEqual(manifest["parameter_grid_sha256"], parameter_grid_digest(manifest["parameter_grid"]))
        self.assertEqual(manifest["manifest_sha256"], manifest_digest(manifest))
        self.assertEqual(manifest["candidate_count"], 2)

    def test_unregistered_parameter_and_identity_changes_are_blockers(self):
        manifest = registered_manifest()
        report = context(manifest, parameters={"edge_buffer": "0.02", "latency_ms": 1000, "order_size": "5", "order_ttl_ms": 5000,
                                               "train_duration_ms": 3600000, "test_duration_ms": 900000, "purge_ms": 300000,
                                               "embargo_ms": 1000, "min_training_events": 10, "calibration_bins": 10})
        self.assertIn("parameter_set_not_registered", report["blockers"])
        report = context(manifest, code_commit="0" * 40)
        self.assertIn("code_commit_mismatch", report["blockers"])
        report = context(manifest, observations_sha256="0" * 64)
        self.assertIn("observation_data_hash_mismatch", report["blockers"])

    def test_post_hoc_selection_and_missing_pbo_cscv_are_blocked(self):
        manifest = registered_manifest()
        report = context(
            manifest,
            phase="post_evaluation",
            selection_stage="test_tuned_after_inspection",
            selection_metric_source="test",
        )
        self.assertIn("post_hoc_model_selection", report["blockers"])
        self.assertIn("test_data_used_for_selection", report["blockers"])
        self.assertIn("pbo_not_computed", report["blockers"])
        self.assertIn("cscv_not_computed", report["blockers"])

    def test_valid_post_evaluation_context_has_no_selection_blockers(self):
        manifest = registered_manifest()
        report = context(
            manifest,
            phase="post_evaluation",
            selection_stage="validation_only_pre_registered",
            selection_metric="brier",
            selection_metric_source="validation",
            pbo_probability=0.2,
            cscv_result={"method": "CSCV", "partitions": 4, "pbo_probability": 0.2},
        )
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["canary_blocked"])
        self.assertIsNone(report["metrics"]["oos"])

    def test_manifest_mutation_fails_digest(self):
        manifest = registered_manifest()
        manifest["hypothesis"] += " changed"
        self.assertIn("manifest_digest_mismatch", validate_manifest(manifest))

    def test_non_object_manifest_fails_closed(self):
        report = audit_manifest([], phase="pre_evaluation")
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any(reason.startswith("manifest:") for reason in report["blockers"]))

    def test_synthetic_cli_is_blocked_without_oos_metrics(self):
        manifest = registered_manifest()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            self.assertEqual(main([
                "--manifest", str(MANIFEST_PATH), "--phase", "pre_evaluation",
                "--observations", str(OBSERVATIONS), "--labels", str(LABELS),
                "--code-commit", CODE_COMMIT, "--evaluation-dates", "2026-10-03",
                "--synthetic", "--output", str(output),
            ]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["manifest_sha256"], manifest["manifest_sha256"])
        self.assertIn("synthetic_input_not_evidence", report["blockers"])
        self.assertEqual(report["metrics"], {"brier": None, "ece": None, "net_pnl_usdc": None, "oos": None})


if __name__ == "__main__":
    unittest.main()
