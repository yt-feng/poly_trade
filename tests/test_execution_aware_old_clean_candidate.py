import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from execution_aware_old_clean_candidate import CANDIDATES, add_signals, candidate_name


class ExecutionAwareOldCleanCandidateTests(unittest.TestCase):
    def test_candidate_buffers_are_fixed_and_named(self):
        self.assertEqual([candidate_name(value) for _, value in CANDIDATES], [name for name, _ in CANDIDATES])
        self.assertEqual([value for _, value in CANDIDATES], [0.0, 0.01, 0.02])

    def test_cost_floor_blocks_insufficient_edge(self):
        frame = pd.DataFrame({
            "pred_prob_up_logit": [0.53, 0.60],
            "buy_up_price_2m": [0.50, 0.50],
            "buy_down_price_2m": [0.50, 0.50],
            "spread_up_median_first2m": [0.02, 0.02],
            "spread_down_median_first2m": [0.02, 0.02],
            "buy_up_size_2m": [200.0, 200.0],
            "buy_down_size_2m": [200.0, 200.0],
            "overround_median_first2m": [0.01, 0.01],
        })
        result = add_signals(frame, "fee_spread_floor")
        self.assertEqual(result["fee_spread_floor_side"].tolist(), ["skip", "buy_up"])
        self.assertEqual(result["fee_spread_floor_minute"].tolist(), [-1, 2])


if __name__ == "__main__":
    unittest.main()
