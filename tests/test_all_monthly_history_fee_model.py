import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import all_monthly_history_strategy_research as study


class AllMonthlyHistoryFeeModelTests(unittest.TestCase):
    def setUp(self):
        self.row = pd.Series({
            "buy_up_price_m1": 0.50,
            "buy_up_size_m1": 100.0,
            "outcome_up": 1.0,
        })

    def test_default_taker_fee_uses_price_curve(self):
        price, size, pnl = study.payout(self.row, "buy_up", 1, 0.07)
        self.assertEqual((price, size), (0.50, 100.0))
        self.assertAlmostEqual(pnl, 0.4825, places=8)

    def test_flat_fee_is_explicit_legacy_mode(self):
        _, _, pnl = study.payout(self.row, "buy_up", 1, 0.07, fee_mode="flat_per_share")
        self.assertAlmostEqual(pnl, 0.43, places=8)


if __name__ == "__main__":
    unittest.main()
