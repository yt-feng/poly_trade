import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from v1_execution_walk_forward import trade_fee_usd


class PolymarketFeeModelTests(unittest.TestCase):
    def test_crypto_taker_fee_matches_documented_half_probability_table(self):
        self.assertEqual(trade_fee_usd(100, 0.50, "polymarket_taker", 0.07, 1), 1.75)

    def test_crypto_taker_fee_matches_documented_ten_cent_table(self):
        self.assertEqual(trade_fee_usd(100, 0.10, "polymarket_taker", 0.07, 1), 0.63)

    def test_flat_fee_mode_remains_explicit_for_legacy_studies(self):
        self.assertEqual(trade_fee_usd(100, 0.50, "flat_per_share", 0.01, 1), 1.0)

    def test_invalid_fee_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            trade_fee_usd(100, 0.50, "unknown", 0.07, 1)


if __name__ == "__main__":
    unittest.main()
