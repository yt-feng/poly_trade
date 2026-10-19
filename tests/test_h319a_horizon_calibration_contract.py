import unittest

from analysis.h319a_horizon_calibration_contract import validate_h319a_rows


def _rows(blocks=5, rows_per_block=10):
    rows = []
    for block in range(blocks):
        for offset in range(rows_per_block):
            decision = 1_700_000_000_000 + (block * rows_per_block + offset) * 301_000
            rows.append({
                "market_id": f"m-{block}-{offset}",
                "decision_ts_ms": decision,
                "scheduled_end_ts_ms": decision + 300_000,
                "time_to_expiry_seconds": 300.0,
                "ptb_received_ts_ms": decision - 2_000,
                "reference_event_ts_ms": decision - 1_000,
                "reference_received_ts_ms": decision - 500,
                "reference_source": "chainlink_twap_60",
                "source_version": "polybolt-v2",
                "market_probability": 0.5,
                "outcome": (block + offset) % 2,
                "outcome_available_ts_ms": decision + 300_000,
                "block_id": f"b-{block}",
            })
    return rows


class H319AHorizonCalibrationContractTests(unittest.TestCase):
    def test_accepts_causal_rows_with_block_floor(self):
        result = validate_h319a_rows(_rows())
        self.assertTrue(result["passed"])
        self.assertEqual(result["row_count"], 50)
        self.assertTrue(result["causal_clock_contract"])

    def test_rejects_future_reference_and_early_label(self):
        rows = _rows()
        rows[0]["reference_received_ts_ms"] = rows[0]["decision_ts_ms"] + 1
        rows[1]["outcome_available_ts_ms"] = rows[1]["scheduled_end_ts_ms"] - 1
        result = validate_h319a_rows(rows)
        self.assertFalse(result["passed"])
        self.assertIn("row_0:reference_received_after_decision", result["failure_reasons"])
        self.assertIn("row_1:outcome_available_before_end", result["failure_reasons"])
        self.assertFalse(result["causal_clock_contract"])

    def test_rejects_duplicate_market_and_insufficient_blocks(self):
        rows = _rows(blocks=2)
        rows[1]["market_id"] = rows[0]["market_id"]
        result = validate_h319a_rows(rows)
        self.assertFalse(result["passed"])
        self.assertIn("row_1:duplicate_market_id", result["failure_reasons"])
        self.assertIn("insufficient_chronological_blocks", result["failure_reasons"])

    def test_rejects_time_to_expiry_mismatch_and_mixed_sources(self):
        rows = _rows()
        rows[0]["time_to_expiry_seconds"] = 60.0
        rows[1]["reference_source"] = "legacy_rtds"
        result = validate_h319a_rows(rows)
        self.assertFalse(result["passed"])
        self.assertIn("row_0:time_to_expiry_mismatch", result["failure_reasons"])
        self.assertIn("mixed_reference_sources", result["failure_reasons"])


if __name__ == "__main__":
    unittest.main()
