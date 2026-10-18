import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from causal_robust_gate_sweep import attach_robust_gate


class CausalRobustGateTests(unittest.TestCase):
    def frame(self, outcomes):
        rows = []
        for i, outcome in enumerate(outcomes):
            rows.append({
                "first_quote_ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=i),
                "classic_early_drop_down_side": "buy_down",
                "classic_early_drop_down_minute": 1,
                "classic_early_drop_down_fraction": 0.04,
                "outcome_up": float(outcome),
                "exec_buy_down_1m_d4_quote_price": 0.50,
                "exec_buy_down_1m_d4_quote_size": 200.0,
                "exec_buy_down_1m_d4_quote_spread": 0.01,
                "exec_buy_down_1m_d4_quote_overround": 0.01,
                "exec_buy_down_1m_d4_adverse_slippage": 0.0,
            })
        return pd.DataFrame(rows)

    def test_lcb_gate_uses_prior_history_only(self):
        frame = self.frame([0.0] * 21)
        allowed, _ = attach_robust_gate(frame, "lcb_half_se", min_history=20, lookback=20)
        blocked, _ = attach_robust_gate(self.frame([0.0] * 20 + [1.0]), "lcb_half_se", min_history=20, lookback=20)
        name = "classic_early_drop_down_lcb_half_se_side"
        self.assertEqual(allowed.iloc[20][name], "buy_down")
        self.assertEqual(blocked.iloc[20][name], "buy_down")

    def test_lower_quartile_gate_blocks_bad_prior_tail(self):
        outcomes = [0.0] * 8 + [1.0] * 12 + [0.0]
        gated, _ = attach_robust_gate(self.frame(outcomes), "lower_quartile_positive", min_history=20, lookback=20)
        name = "classic_early_drop_down_lower_quartile_positive_side"
        self.assertEqual(gated.iloc[20][name], "skip")


if __name__ == "__main__":
    unittest.main()
