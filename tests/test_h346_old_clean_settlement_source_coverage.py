import unittest

from analysis.h346_old_clean_settlement_source_coverage import classify_headers, summarize_rows


class H346CoverageTests(unittest.TestCase):
    def test_projection_schema_has_no_settlement_provenance(self):
        report = classify_headers({"ts_iso", "slug", "target_price", "final_price"})
        self.assertFalse(report["twap_fields_present"])
        self.assertFalse(report["provider_fields_present"])
        self.assertFalse(report["sequence_fields_present"])
        self.assertFalse(report["ptb_receipt_clock_present"])

    def test_current_reference_is_observed_before_window_end(self):
        rows = [
            {"slug": "btc-updown-5m-1800000000", "ts_iso": "2027-01-15T08:00:00+00:00", "target_price": "", "final_price": "100"},
            {"slug": "btc-updown-5m-1800000000", "ts_iso": "2027-01-15T08:05:00+00:00", "target_price": "101", "final_price": "100"},
        ]
        result = summarize_rows(rows)
        self.assertEqual(result["target_first_seen_before_start_markets"], 0)
        self.assertEqual(result["reference_pre_end_markets"], 1)
        self.assertEqual(result["reference_pre_end_rows"], 1)


if __name__ == "__main__":
    unittest.main()
