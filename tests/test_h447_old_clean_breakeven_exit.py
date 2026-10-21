import unittest

import pandas as pd

from analysis.h447_old_clean_breakeven_exit import _first_break_even_exit, _replay


class H447BreakevenExitTests(unittest.TestCase):
    def candidate(self, **overrides):
        values = {
            "slug": "btc-updown-5m-1000",
            "run_name": "r1",
            "entry_ts": pd.Timestamp("1970-01-01T00:02:04Z"),
            "settlement_ts": pd.Timestamp("1970-01-01T00:00:00Z"),
            "cash_release_ts": pd.Timestamp("1970-01-01T00:15:00Z"),
            "side": "down",
            "q_side": 0.85,
            "price": 0.68,
            "fill_price": 0.685,
            "shares": 5.0 / 0.685,
            "cost": 5.0,
            "fee": 0.11025,
            "outcome_up": 1.0,
            "slippage": 0.005,
        }
        values.update(overrides)
        return pd.DataFrame([values]).itertuples(index=False).__next__()

    def test_exit_requires_full_displayed_size_and_covers_both_fees(self):
        row = self.candidate()
        quotes = pd.DataFrame([
            {"ts_iso": pd.Timestamp("1970-01-01T00:02:05Z"), "sell_down_cents": 74.0, "sell_down_size": 1.0, "sell_up_cents": 26.0, "sell_up_size": 100.0},
            {"ts_iso": pd.Timestamp("1970-01-01T00:02:06Z"), "sell_down_cents": 74.0, "sell_down_size": 100.0, "sell_up_cents": 26.0, "sell_up_size": 100.0},
        ])
        result = _first_break_even_exit(row, quotes)
        self.assertTrue(result["exit_used"])
        self.assertEqual(result["exit_ts"], "1970-01-01T00:02:06+00:00")
        self.assertGreaterEqual(result["exit_payout"], row.cost + row.fee - 1e-9)

    def test_exit_does_not_use_quotes_after_market_end(self):
        row = self.candidate()
        quotes = pd.DataFrame([
            {"ts_iso": pd.Timestamp("1970-01-01T00:05:01Z"), "sell_down_cents": 99.0, "sell_down_size": 100.0, "sell_up_cents": 1.0, "sell_up_size": 100.0},
        ])
        result = _first_break_even_exit(row, quotes)
        self.assertFalse(result["exit_used"])

    def test_replay_requires_one_evolving_ledger_and_keeps_release_clock(self):
        row = self.candidate()
        frame = pd.DataFrame([row._asdict()])
        frame["exit_used"] = True
        frame["exit_payout"] = 5.3
        frame["exit_ts"] = "1970-01-01T00:02:06+00:00"
        frame["exit_fill_price"] = 0.74
        result = _replay(frame, ["r1"], early_exit=True)
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["first_ten_early_exits"], 1)
        self.assertEqual(result["failure_reason"], "fewer_than_required_consecutive_fills")
        self.assertAlmostEqual(result["ending_cash"], 10.18975, places=6)


if __name__ == "__main__":
    unittest.main()
