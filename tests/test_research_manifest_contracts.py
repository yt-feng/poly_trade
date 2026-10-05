import json
from pathlib import Path
import tempfile
import unittest

from analysis.preregistered_strategy import manifest_digest
from analysis.research_manifest_contracts import (
    validate_experiment_directory,
    validate_experiment_manifest,
)


ROOT = Path(__file__).parents[1]
EXPERIMENTS = ROOT / "research" / "strategy" / "experiments"
LEGACY = "EXP-0001-canary-foundation.json"
TYPED = "EXP-0002-btc5m-preregistered-reference.json"


def record(name):
    return json.loads((EXPERIMENTS / name).read_text(encoding="utf-8"))


class ResearchManifestContractTests(unittest.TestCase):
    def test_actual_mixed_experiment_directory_validates_without_rewriting_identities(self):
        before = {path.name: path.read_bytes() for path in EXPERIMENTS.glob("*.json")}
        paths = validate_experiment_directory(EXPERIMENTS)
        self.assertIn(EXPERIMENTS / LEGACY, paths)
        self.assertIn(EXPERIMENTS / TYPED, paths)
        self.assertNotIn("experiment_id", record(TYPED))
        self.assertEqual(record(TYPED)["strategy_id"], "btc5m_reference_v1")
        self.assertEqual(before, {path.name: path.read_bytes() for path in paths})

    def test_typed_manifest_checks_digest_and_complete_existing_contract(self):
        changed = record(TYPED)
        changed["hypothesis"] += " changed"
        self.assertIn("manifest_digest_mismatch", validate_experiment_manifest(changed))
        for field, value, expected in (
            ("protocol", {"pre_registered": True, "public_quotes_are_not_fills": True, "real_orders": True}, "protocol_real_orders_must_be_false"),
            ("strategy_id", "", "invalid_strategy_id"),
            ("candidate_count", 99, "parameter_candidate_count_mismatch"),
        ):
            with self.subTest(field=field):
                changed = record(TYPED)
                changed[field] = value
                changed["manifest_sha256"] = manifest_digest(changed)
                self.assertIn(expected, validate_experiment_manifest(changed))

    def test_unknown_type_cannot_fall_back_to_legacy_contract(self):
        for unknown in ("unrecognized_strategy", None, ""):
            with self.subTest(unknown=unknown):
                changed = record(LEGACY)
                changed["manifest_type"] = unknown
                self.assertEqual(validate_experiment_manifest(changed), ["unsupported_experiment_manifest_type"])

    def test_typed_record_missing_discriminator_cannot_become_legacy(self):
        changed = record(TYPED)
        del changed["manifest_type"]
        changed.update(experiment_id="spoofed", scope="strategy_research")
        self.assertEqual(validate_experiment_manifest(changed), ["missing_experiment_manifest_type"])

    def test_resigned_strategy_cannot_disable_selection_or_missing_evidence_gates(self):
        for section, field, value, expected in (
            ("selection_rule", "uses_test_data", True, "selection_test_data_forbidden"),
            ("multiple_testing", "block_on_missing", False, "multiple_testing_must_block_on_missing"),
        ):
            with self.subTest(section=section, field=field):
                changed = record(TYPED)
                changed[section][field] = value
                changed["manifest_sha256"] = manifest_digest(changed)
                self.assertIn(expected, validate_experiment_manifest(changed))

    def test_legacy_identity_scope_and_quote_boundary_remain_required(self):
        for field, value, expected in (
            ("experiment_id", "", "missing_experiment_id"),
            ("scope", "production", "invalid_experiment_scope"),
            ("protocol", {"public_quotes_are_not_fills": False}, "experiment_public_quote_boundary_missing"),
        ):
            with self.subTest(field=field):
                changed = record(LEGACY)
                changed[field] = value
                self.assertIn(expected, validate_experiment_manifest(changed))

    def test_directory_reports_bad_record_among_valid_records_instead_of_skipping_it(self):
        for invalid in ("{not json", "[]", json.dumps({"manifest_type": "unknown"})):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path / LEGACY).write_text(json.dumps(record(LEGACY)))
                (path / TYPED).write_text(json.dumps(record(TYPED)))
                (path / "EXP-9999-invalid.json").write_text(invalid)
                with self.assertRaisesRegex(ValueError, "EXP-9999-invalid.json"):
                    validate_experiment_directory(path)

    def test_empty_directory_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no experiment manifests"):
                validate_experiment_directory(Path(directory))

    def test_malformed_nested_types_fail_with_manifest_filename(self):
        changed = record(TYPED)
        changed["selection_rule"]["direction"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / TYPED).write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, TYPED):
                validate_experiment_directory(path)


if __name__ == "__main__":
    unittest.main()
