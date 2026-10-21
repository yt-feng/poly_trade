import unittest

import pandas as pd

from analysis.h450_old_clean_depth_decay import _first_break_even_exit_cutoff


class H450DepthDecayTests(unittest.TestCase):
    def row(self):
        return type("Row", (), {
            "side": "down", "entry_ts": pd.Timestamp("1970-01-01T00:02:04Z"),
            "settlement_ts": pd.Timestamp("1970-01-01T00:00:00Z"),
            "shares": 10.0, "cost": 6.85, "fee": 0.11025, "slippage": 0.005,
        })()

    def test_cutoff_rejects_only_late_exit(self):
        quotes = pd.DataFrame([
            {"ts_iso": pd.Timestamp("1970-01-01T00:04:40Z"), "sell_down_cents": 80.0, "sell_down_size": 100.0},
        ])
        self.assertTrue(_first_break_even_exit_cutoff(self.row(), quotes, 0.0)["exit_used"])
        self.assertFalse(_first_break_even_exit_cutoff(self.row(), quotes, 60.0)["exit_used"])

    def test_cutoff_keeps_causal_early_exit(self):
        quotes = pd.DataFrame([
            {"ts_iso": pd.Timestamp("1970-01-01T00:02:06Z"), "sell_down_cents": 80.0, "sell_down_size": 100.0},
        ])
        result = _first_break_even_exit_cutoff(self.row(), quotes, 60.0)
        self.assertTrue(result["exit_used"])
        self.assertEqual(result["exit_ts"], "1970-01-01T00:02:06+00:00")


if __name__ == "__main__":
    unittest.main()
