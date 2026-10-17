import importlib.util
import unittest

import numpy as np
import pandas as pd


SPEC = importlib.util.spec_from_file_location(
    "all_monthly_history_strategy_research",
    "analysis/all_monthly_history_strategy_research.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RollingStateIndexTests(unittest.TestCase):
    def test_incremental_index_matches_legacy_state_probability(self):
        rows = []
        for i in range(48):
            rows.append({
                "regime": "a" if i % 3 else "b",
                "move_bucket_m2": "x" if i % 2 else "y",
                "move_bucket_m4": "u" if i % 4 else "v",
                "up_price_bucket_m2": "p" if i % 5 else "q",
                "up_price_bucket_m4": "r" if i % 3 else "s",
                "down_price_bucket_m2": "d" if i % 4 else "e",
                "imb_sign_m2": "neg" if i % 2 else "pos",
                "liq_sign_down_m2": "low" if i % 3 else "high",
                "outcome_up": float((i * 7) % 5 in (0, 1)),
            })
        frame = pd.DataFrame(rows)
        index = MODULE.RollingStateIndex()
        for i, row in frame.iterrows():
            for side in ("buy_up", "buy_down"):
                expected = MODULE.state_prob(frame.iloc[:i], row, side) if i else (np.nan, 0)
                actual = index.probability(row, side)
                self.assertEqual(expected[1], actual[1])
                if expected[1]:
                    self.assertAlmostEqual(expected[0], actual[0], places=12)
            index.update(row)


if __name__ == "__main__":
    unittest.main()
