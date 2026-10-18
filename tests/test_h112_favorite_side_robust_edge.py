import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from h112_favorite_side_robust_edge import validate_headers


class H112SchemaGateTests(unittest.TestCase):
    def test_old_clean_like_header_rejects_without_independent_price(self):
        result = validate_headers({
            "ts_iso", "buy_up_cents", "buy_down_cents", "buy_up_size",
            "buy_down_size", "target_price", "final_price",
        })
        self.assertFalse(result["passed"])
        self.assertIn("independent_spot_or_chainlink_price", result["missing"])
        self.assertTrue(result["outcome_fields_rejected_as_fair_value"])

    def test_complete_causal_header_passes(self):
        result = validate_headers({
            "ts_iso", "buy_up_cents", "buy_down_cents", "buy_up_size",
            "buy_down_size", "spot_price", "window_start_ts", "window_end_ts",
        })
        self.assertTrue(result["passed"])
        self.assertEqual(result["independent_price_fields"], ["spot_price"])
        self.assertEqual(result["time_alignment_fields"], ["window_end_ts", "window_start_ts"])

    def test_final_price_is_never_accepted_as_fair_value(self):
        result = validate_headers({
            "ts_iso", "buy_up_cents", "buy_down_cents", "ask_depth_up_5",
            "final_price", "window_start_ts", "window_end_ts",
        })
        self.assertFalse(result["passed"])
        self.assertEqual(result["independent_price_fields"], [])


if __name__ == "__main__":
    unittest.main()
