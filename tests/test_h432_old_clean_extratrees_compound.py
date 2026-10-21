import unittest

import pandas as pd

from analysis.h432_old_clean_extratrees_compound import replay_chain


def row(index: int, *, win: bool = True, entry_offset: int | None = None) -> dict:
    entry_offset = (index * 1000) if entry_offset is None else entry_offset
    entry = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(seconds=entry_offset)
    release = entry + pd.Timedelta(seconds=900)
    return {
        "slug": f"btc-updown-5m-{index}",
        "run_name": "run-a",
        "entry_ts": entry,
        "cash_release_ts": release,
        "side": "up",
        "q_side": 0.8,
        "price": 0.4,
        "fill_price": 0.405,
        "cost": 5.0,
        "fee": 0.0163,
        "outcome_up": 1.0 if win else 0.0,
        "shares": 12.5,
        "slippage": 0.005,
    }


class H432CompoundReplayTests(unittest.TestCase):
    def test_next_entry_before_release_is_not_counted(self):
        candidates = pd.DataFrame([row(0), row(1, entry_offset=600), row(2, entry_offset=1000)])
        result = replay_chain(candidates, ["run-a"])
        self.assertEqual(result["fills"], 2)
        self.assertEqual(result["blocked_before_release"], 1)

    def test_loss_ends_chain_after_release(self):
        candidates = pd.DataFrame([row(0, win=False), row(1, entry_offset=1000)])
        result = replay_chain(candidates, ["run-a"])
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["failure_reason"], "prior_loss_ends_chain")
        self.assertFalse(result["first_ten_passed"])

    def test_ten_wins_on_one_released_cash_ledger_pass(self):
        candidates = pd.DataFrame([row(i) for i in range(10)])
        result = replay_chain(candidates, ["run-a"])
        self.assertEqual(result["first_ten_fills"], 10)
        self.assertTrue(result["first_ten_passed"])
        self.assertGreater(result["first_ten_ending_cash"], 10.0)


if __name__ == "__main__":
    unittest.main()
