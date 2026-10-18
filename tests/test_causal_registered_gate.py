import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from causal_registered_gate_sweep import attach_gate


class CausalRegisteredGateTests(unittest.TestCase):
    def frame(self, current_outcome):
        rows = []
        for i in range(21):
            rows.append({
                "first_quote_ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=i),
                "classic_early_drop_down_side": "buy_down",
                "classic_early_drop_down_minute": 1,
                "classic_early_drop_down_fraction": 0.04,
                "outcome_up": 0.0 if i < 20 else current_outcome,
                "exec_buy_down_1m_d4_quote_price": 0.50,
                "exec_buy_down_1m_d4_quote_size": 200.0,
                "exec_buy_down_1m_d4_quote_spread": 0.01,
                "exec_buy_down_1m_d4_quote_overround": 0.01,
                "exec_buy_down_1m_d4_adverse_slippage": 0.0,
            })
        return pd.DataFrame(rows)

    def test_current_outcome_cannot_change_current_gate(self):
        positive, _ = attach_gate(self.frame(0.0), "classic_early_drop_down", min_history=20, lookback=20)
        negative, _ = attach_gate(self.frame(1.0), "classic_early_drop_down", min_history=20, lookback=20)
        name = "classic_early_drop_down_history_net_positive_side"
        self.assertEqual(positive.iloc[20][name], "buy_down")
        self.assertEqual(negative.iloc[20][name], "buy_down")


if __name__ == "__main__":
    unittest.main()
