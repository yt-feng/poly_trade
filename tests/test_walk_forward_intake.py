import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from analysis.walk_forward_intake import build_report, main, sha256_file


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class WalkForwardIntakeTests(unittest.TestCase):
    def test_synthetic_valid_fixture_is_hashed_but_never_evidence(self):
        observations = FIXTURES / "v3_observation_valid.jsonl"
        labels = FIXTURES / "walk_forward_labels_valid.jsonl"
        report = build_report(
            observations,
            labels,
            expected_observations_sha256=sha256_file(observations),
            expected_labels_sha256=sha256_file(labels),
            synthetic=True,
        )
        self.assertEqual(report["files"]["observations"]["sha256_match"], True)
        self.assertEqual(report["files"]["labels"]["sha256_match"], True)
        self.assertEqual(report["observations"]["accepted_book_records"], 1)
        self.assertEqual(report["observations"]["source_sha256_verification"], "format_only_without_source_bytes")
        self.assertEqual(report["coverage"]["market_count"], 1)
        self.assertEqual(report["coverage"]["condition_count"], 1)
        self.assertEqual(report["coverage"]["token_counts"], {"up": 1, "down": 1})
        self.assertEqual(len(report["coverage"]["coverage_sha256"]["market_ids"]), 64)
        self.assertEqual(report["labels"]["accepted_records"], 1)
        self.assertEqual(report["observations"]["contract"]["schema_version"], 3)
        self.assertIn("source_sha256", report["labels"]["contract"]["required_fields"])
        self.assertTrue(report["synthetic_input"])
        self.assertFalse(report["evidence_qualifies"])
        self.assertFalse(report["canary_allowed"])
        self.assertIn("synthetic_input_not_evidence", report["blocked_reasons"])
        self.assertIn("fewer_than_300_independent_windows", report["blocked_reasons"])
        self.assertIsNone(report["metrics"]["brier"])

    def test_invalid_label_fixture_is_quarantined_by_field_and_availability(self):
        report = build_report(
            FIXTURES / "v3_observation_valid.jsonl",
            FIXTURES / "walk_forward_labels_invalid.jsonl",
        )
        self.assertEqual(report["labels"]["accepted_records"], 0)
        self.assertEqual(report["labels"]["quarantined_records"], 2)
        reasons = report["labels"]["reason_counts"]
        self.assertIn("label_available_before_resolution", reasons)
        self.assertIn("unknown_or_missing_label_fields", reasons)
        self.assertIn("quarantined_label_records", report["blocked_reasons"])
        self.assertIn("no_valid_resolution_labels", report["blocked_reasons"])

    def test_label_known_before_observation_is_a_leakage_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            labels = Path(directory) / "labels.jsonl"
            labels.write_text(json.dumps({
                "label_version": 1,
                "market_id": "btc-updown-5m-1791000000",
                "condition_id": "condition-001",
                "outcome": "up",
                "resolved_time_ms": 1790999999000,
                "label_available_time_ms": 1791000000000,
                "source": "leak-fixture",
                "source_sha256": "a" * 64,
            }) + "\n", encoding="utf-8")
            report = build_report(FIXTURES / "v3_observation_valid.jsonl", labels)
        self.assertIn("label_available_before_or_at_observation", report["blocked_reasons"])
        self.assertIn("observation_at_or_after_resolution", report["blocked_reasons"])
        self.assertGreater(report["alignment"]["label_known_before_or_at_observation_count"], 0)
        self.assertIsNone(report["metrics"]["net_pnl_usdc"])

    def test_expected_file_hash_mismatch_is_blocked_without_reading_metrics(self):
        report = build_report(
            FIXTURES / "v3_observation_valid.jsonl",
            FIXTURES / "walk_forward_labels_valid.jsonl",
            expected_observations_sha256="0" * 64,
        )
        self.assertIn("observation_file_sha256_mismatch", report["blocked_reasons"])
        self.assertIsNone(report["metrics"]["oos"])

    def test_market_condition_or_token_identity_conflict_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            observations = Path(directory) / "observations.jsonl"
            first = json.loads((FIXTURES / "v3_observation_valid.jsonl").read_text(encoding="utf-8"))
            second = dict(first)
            second["observation_id"] = "obs-conflicting-identity"
            second["source_event_time_ms"] += 100
            second["received_time_ms"] += 100
            second["condition_id"] = "condition-conflict"
            second["token_ids"] = {"up": "token-up-conflict", "down": "token-down-conflict"}
            observations.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
            report = build_report(observations, FIXTURES / "walk_forward_labels_valid.jsonl")
        self.assertIn("inconsistent_market_condition_or_token_identity", report["blocked_reasons"])
        self.assertIn("label_condition_mismatch", report["blocked_reasons"])
        self.assertEqual(report["coverage"]["inconsistent_market_identity_count"], 1)

    def test_cli_writes_public_safe_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "intake.json"
            result = main([
                "--observations", str(FIXTURES / "v3_observation_valid.jsonl"),
                "--labels", str(FIXTURES / "walk_forward_labels_valid.jsonl"),
                "--synthetic", "--output", str(output),
            ])
            self.assertEqual(result, 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(report["canary_blocked"])
            digest = report["manifest_sha256"]
            unsigned = dict(report)
            unsigned.pop("manifest_sha256")
            canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())
            encoded = output.read_text(encoding="utf-8")
            self.assertNotIn(str(ROOT), encoded)
            self.assertNotIn("btc-updown-5m-1791000000", encoded)


if __name__ == "__main__":
    unittest.main()
