import unittest

import pandas as pd

from analysis.h329_old_clean_cross_candidate_reconciliation import summarize_candidate


def rows(candidate, cash=6.0, fills=10, passed=True):
    return pd.DataFrame([
        {
            "candidate": candidate,
            "split": f"holdout_block_{i}",
            "fee_rate": 0.07,
            "cash_release_delay_seconds": 600.0,
            "min_order_notional": 5.0,
            "min_order_shares": 5.0,
            "settlement_seconds": 300.0,
            "slippage": 0.005,
            "depth_floor": 100.0,
            "first_ten_fills": fills,
            "first_ten_ending_cash_usd": 12.0,
            "first_ten_minimum_cash_usd": cash,
            "first_ten_passed": passed,
            "paper_chain_fills": fills,
            "paper_chain_ending_cash_usd": 12.0,
            "paper_chain_minimum_cash_usd": cash,
            "paper_chain_passed": passed,
        }
        for i in range(1, 6)
    ])


class H329Tests(unittest.TestCase):
    def test_cash_floor_admits_stable_candidate(self):
        result = summarize_candidate(rows("candidate"), "candidate")
        self.assertTrue(result["cash_floor_passed"])
        self.assertTrue(result["admission_passed"])

    def test_cash_floor_rejects_paper_prefix(self):
        result = summarize_candidate(rows("candidate", cash=4.99), "candidate")
        self.assertFalse(result["cash_floor_passed"])
        self.assertFalse(result["admission_passed"])


if __name__ == "__main__":
    unittest.main()
