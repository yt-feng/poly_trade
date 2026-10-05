import json
from pathlib import Path
import unittest

from analysis.public_acquisition_dry_run import BLOCKED_REASONS, build_report


ROOT = Path(__file__).parents[1]
REPORT_PATH = ROOT / "reports" / "walk_forward_execution" / "public_acquisition_dry_run_blocked_20261005.json"
HANDOFF_PATH = ROOT / "research" / "strategy" / "runs" / "public_v3_data_handoff_request.json"


class PublicAcquisitionDryRunTests(unittest.TestCase):
    def test_checked_in_report_is_deterministic_and_fail_closed(self):
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(report, build_report())
        self.assertTrue(report["canary_blocked"])
        self.assertFalse(report["canary_allowed"])
        self.assertFalse(report["evidence_qualifies"])
        self.assertEqual(report["blocked_reasons"], list(BLOCKED_REASONS))
        self.assertEqual(report["observations"]["accepted_canonical_v3_records"], 0)
        self.assertEqual(report["labels"]["independent_resolution_labels"], 0)
        self.assertIsNone(report["metrics"]["oos"])
        self.assertIsNone(report["metrics"]["net_pnl_usdc"])

    def test_report_contains_no_local_paths_or_secrets(self):
        text = REPORT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("/workspace/", text)
        self.assertNotIn("4121", text)

    def test_handoff_requests_canonical_observations_and_independent_labels(self):
        handoff = json.loads(HANDOFF_PATH.read_text(encoding="utf-8"))
        self.assertEqual(handoff["required_observation_contract"], "research/strategy/schema/v3_observation.schema.json")
        self.assertEqual(handoff["required_label_contract"], "research/strategy/schema/walk_forward_label.schema.json")
        self.assertTrue(handoff["observation_minimum"]["midpoint_only_forbidden"])
        self.assertTrue(handoff["label_minimum"]["official_source_required"])
        self.assertIn("quote_markout_not_fill_pnl", handoff["forbidden_substitutes"])
        self.assertTrue(handoff["safety"]["no_orders"])


if __name__ == "__main__":
    unittest.main()
