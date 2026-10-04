import json
import unittest
from pathlib import Path

from analysis.v3_data_contract import alignment_report, load_jsonl, missingness_report, validate_record, validate_records


FIXTURES = Path(__file__).parent / "fixtures"


class V3DataContractTests(unittest.TestCase):
    def test_valid_snapshot_requires_and_accepts_execution_metadata(self):
        record = load_jsonl(FIXTURES / "v3_observation_valid.jsonl")[0]
        ok, reasons = validate_record(record)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_missing_rules_and_future_label_are_quarantined(self):
        report = validate_records(load_jsonl(FIXTURES / "v3_observation_invalid.jsonl"))
        self.assertEqual(report["input_records"], 2)
        self.assertEqual(report["accepted_records"], 0)
        self.assertEqual(report["quarantined_records"], 2)
        self.assertIn("invalid_rules", report["reason_counts"])
        self.assertIn("forbidden_future_field:target_price", report["reason_counts"])
        self.assertFalse(report["leakage_check"]["passed"])
        self.assertEqual(report["missingness"]["rules"], 1)
        self.assertEqual(report["missingness"]["source_event_time_ms"], 0)

    def test_alignment_reports_latency_and_rejects_future_event(self):
        record = load_jsonl(FIXTURES / "v3_observation_valid.jsonl")[0]
        report = alignment_report([record, {"source_event_time_ms": 2000, "received_time_ms": 1000}])
        self.assertEqual(report["records_with_both_timestamps"], 2)
        self.assertEqual(report["future_event_records"], 1)
        self.assertEqual(report["median_latency_ms"], -440)
        bad = dict(record, source_event_time_ms=record["received_time_ms"] + 3000)
        ok, reasons = validate_record(bad)
        self.assertFalse(ok)
        self.assertIn("source_event_after_receive", reasons)

    def test_duplicate_and_conflicting_ids_are_quarantined(self):
        record = load_jsonl(FIXTURES / "v3_observation_valid.jsonl")[0]
        duplicate = json.loads(json.dumps(record))
        conflict = json.loads(json.dumps(record))
        conflict["received_time_ms"] += 1
        report = validate_records([record, duplicate, conflict])
        self.assertEqual(report["accepted_records"], 1)
        self.assertEqual(report["reason_counts"]["duplicate_observation_id"], 1)
        self.assertEqual(report["reason_counts"]["conflicting_duplicate_observation_id"], 1)

    def test_unknown_top_level_metadata_is_quarantined(self):
        record = load_jsonl(FIXTURES / "v3_observation_valid.jsonl")[0]
        record["unregistered_metadata"] = "must be registered"
        ok, reasons = validate_record(record)
        self.assertFalse(ok)
        self.assertIn("unknown_top_level_field:unregistered_metadata", reasons)


if __name__ == "__main__":
    unittest.main()
