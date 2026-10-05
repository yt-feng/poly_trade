import json
from pathlib import Path
import tempfile
import unittest

from analysis.walk_forward_split_audit import SplitConfig, audit_splits, build_report, main


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "v3_observation_valid.jsonl"
LABEL_FIXTURE = ROOT / "tests" / "fixtures" / "walk_forward_labels_valid.jsonl"


def observation(market: str, when: int) -> dict:
    row = json.loads(FIXTURE.read_text(encoding="utf-8"))
    row["observation_id"] = f"obs-{market}-{when}"
    row["market_id"] = market
    row["condition_id"] = f"condition-{market}"
    row["token_ids"] = {"up": f"up-{market}", "down": f"down-{market}"}
    row["source_event_time_ms"] = when
    row["received_time_ms"] = when + 100
    row["provenance"] = dict(row["provenance"], capture_id="split-audit-fixture")
    return row


def label(market: str, condition: str, available: int) -> dict:
    return {
        "label_version": 1,
        "market_id": market,
        "condition_id": condition,
        "outcome": "up",
        "resolved_time_ms": available - 100,
        "label_available_time_ms": available,
        "source": "split-audit-fixture",
        "source_sha256": "b" * 64,
    }


def package_rows(extra_markets: list[tuple[str, int]] | None = None):
    markets = [
        ("m0", 1_000_000, 1_001_500),
        ("m1", 1_001_000, 1_001_500),
        ("m2", 1_002_200, 1_004_100),
        ("m3", 1_003_300, 1_005_100),
    ]
    if extra_markets:
        markets.extend((market, when, when + 1000) for market, when in extra_markets)
    observations = [observation(market, when) for market, when, _ in markets]
    labels = [label(market, f"condition-{market}", available) for market, _, available in markets]
    return observations, labels


CONFIG = SplitConfig(
    train_duration_ms=2_000,
    validation_duration_ms=1_000,
    test_duration_ms=1_000,
    purge_ms=100,
    embargo_ms=50,
)


class WalkForwardSplitAuditTests(unittest.TestCase):
    def test_market_condition_split_has_explicit_boundaries_and_no_overlap(self):
        observations, labels = package_rows()
        report = audit_splits(observations, {row["market_id"]: row for row in labels}, CONFIG)
        self.assertEqual(report["blockers"], [])
        self.assertEqual(len(report["folds"]), 1)
        fold = report["folds"][0]
        self.assertEqual(fold["counts"]["train"]["clusters"], 2)
        self.assertEqual(fold["counts"]["validation"]["clusters"], 1)
        self.assertEqual(fold["counts"]["test"]["clusters"], 1)
        self.assertEqual(fold["boundaries_ms"]["test_start"], 1_003_250)
        self.assertEqual(fold["label_availability_cutoffs_ms"]["train_must_be_at_or_before"], 1_002_000)
        self.assertEqual(fold["overlap"], {"train_validation": [], "train_test": [], "validation_test": []})

    def test_cluster_in_purge_interval_is_a_blocker(self):
        observations, labels = package_rows([("purge", 1_002_050)])
        report = audit_splits(observations, {row["market_id"]: row for row in labels}, CONFIG)
        self.assertIn("cluster_in_purge_or_embargo_interval", report["blockers"])
        self.assertEqual(report["folds"][0]["counts"]["purge_1"]["clusters"], 1)

    def test_label_available_before_test_feature_is_a_blocker(self):
        observations, labels = package_rows()
        labels[-1]["resolved_time_ms"] = 1_003_000
        labels[-1]["label_available_time_ms"] = 1_003_350
        report = audit_splits(observations, {row["market_id"]: row for row in labels}, CONFIG)
        self.assertIn("test_label_known_before_feature", report["blockers"])
        self.assertEqual(report["folds"][0]["counts"]["test_label_known_before_feature"], 1)

    def test_file_manifest_hash_and_missing_input_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations_path = root / "observations.jsonl"
            labels_path = root / "labels.jsonl"
            observations, labels = package_rows()
            observations_path.write_text("\n".join(json.dumps(row) for row in observations) + "\n", encoding="utf-8")
            labels_path.write_text("\n".join(json.dumps(row) for row in labels) + "\n", encoding="utf-8")
            report = build_report(observations_path, labels_path, config=CONFIG, synthetic=True)
            self.assertTrue(report["files"]["observations"]["present"])
            self.assertTrue(report["files"]["labels"]["present"])
            self.assertTrue(report["manifest_sha256"])
            self.assertIn("synthetic_input_not_evidence", report["blocked_reasons"])
            missing = build_report(root / "missing.jsonl", labels_path, config=CONFIG)
        self.assertIn("missing_observation_file", missing["blocked_reasons"])
        self.assertIsNone(missing["metrics"]["brier"])

    def test_cli_report_contains_no_oos_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations, labels = package_rows()
            observations_path = root / "observations.jsonl"
            labels_path = root / "labels.jsonl"
            output = root / "report.json"
            observations_path.write_text("\n".join(json.dumps(row) for row in observations) + "\n", encoding="utf-8")
            labels_path.write_text("\n".join(json.dumps(row) for row in labels) + "\n", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(CONFIG.__dict__), encoding="utf-8")
            self.assertEqual(main([
                "--observations", str(observations_path), "--labels", str(labels_path),
                "--config", str(config_path), "--synthetic", "--output", str(output),
            ]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["canary_blocked"])
        self.assertIsNone(report["metrics"]["oos"])
        self.assertGreaterEqual(len(report["split_audit"]["folds"]), 1)


if __name__ == "__main__":
    unittest.main()
