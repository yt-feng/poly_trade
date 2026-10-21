import unittest

import pandas as pd

from analysis.h441_h434_loss_tolerant_compound import _cell_summary, replay_chain


def _row(slug, entry, release, outcome, cost=5.0, fee=0.0, shares=10.0):
    return {
        "slug": slug,
        "run_name": "r",
        "entry_ts": pd.Timestamp(entry, tz="UTC"),
        "cash_release_ts": pd.Timestamp(release, tz="UTC"),
        "outcome_up": outcome,
        "side": "up",
        "cost": cost,
        "fee": fee,
        "shares": shares,
        "q_side": 0.9,
        "price": 0.5,
        "fill_price": 0.5,
    }


class H441LossTolerantCompoundTests(unittest.TestCase):
    def test_settled_loss_does_not_end_fundable_chain(self):
        rows = []
        for i in range(10):
            rows.append(_row(
                f"s-{i}",
                f"2026-01-01T00:{i * 5:02d}:00",
                f"2026-01-01T00:{i * 5 + 1:02d}:00",
                0 if i == 1 else 1,
            ))
        result = replay_chain(pd.DataFrame(rows), ["r"])
        self.assertEqual(result["first_ten_fills"], 10)
        self.assertEqual(result["first_ten_wins"], 9)
        self.assertEqual(result["first_ten_losses"], 1)
        self.assertTrue(result["first_ten_passed"])

    def test_unfundable_next_order_is_the_chain_failure(self):
        rows = [
            _row("loss", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", 0),
            _row("next", "2026-01-01T00:02:00Z", "2026-01-01T00:03:00Z", 1, fee=0.2),
        ]
        result = replay_chain(pd.DataFrame(rows), ["r"])
        self.assertEqual(result["first_ten_fills"], 1)
        self.assertEqual(result["failure_reason"], "insufficient_cash_for_minimum_order")
        self.assertFalse(result["first_ten_passed"])

    def test_cell_gate_counts_bool_values(self):
        rows = [
            {"slippage": 0.005, "first_ten_passed": True, "first_ten_ending_cash": 12, "first_ten_fills": 10},
            {"slippage": 0.005, "first_ten_passed": False, "first_ten_ending_cash": 9, "first_ten_fills": 3},
        ]
        summary = _cell_summary(rows, 0.005)
        self.assertEqual(summary["growth_positive_windows"], 1)
        self.assertFalse(summary["robust_window_gate"])


if __name__ == "__main__":
    unittest.main()
