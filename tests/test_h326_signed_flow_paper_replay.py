import gzip
import json
import tempfile
import unittest
from pathlib import Path

from analysis.h326_signed_flow_paper_replay import (
    ENTRY_SECONDS,
    INITIAL_CASH,
    MIN_ORDER_SHARES,
    POSITION_FRACTION,
    replay,
    taker_fee,
)


class H326Tests(unittest.TestCase):
    def test_current_fee_formula_and_share_floor(self):
        self.assertAlmostEqual(taker_fee(5.0, 0.5), 0.0875)
        self.assertGreaterEqual(INITIAL_CASH * POSITION_FRACTION / 0.1, MIN_ORDER_SHARES)

    def test_same_ledger_blocks_until_cash_release_and_counts_no_canary(self):
        events = [
            {"slug": "a", "end": 300, "selected_outcome": "Up", "entry_price": 0.1, "winner": "Up"},
            {"slug": "b", "end": 500, "selected_outcome": "Up", "entry_price": 0.1, "winner": "Up"},
            {"slug": "c", "end": 1000, "selected_outcome": "Down", "entry_price": 0.1, "winner": "Down"},
        ]
        report = replay(events)
        self.assertEqual(report["fills"], 2)
        self.assertEqual(report["blocked_cash_events"], 1)
        self.assertFalse(report["paper_first_ten_passed"])

    def test_missing_signal_is_skipped_without_lookahead(self):
        events = [{"slug": "a", "end": 300, "selected_outcome": None, "entry_price": None, "winner": "Up"}]
        report = replay(events)
        self.assertEqual(report["fills"], 0)
        self.assertEqual(report["blocked_cash_events"], 0)


if __name__ == "__main__":
    unittest.main()
