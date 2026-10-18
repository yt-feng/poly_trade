import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from cash_release_sensitivity import simulate_cash_locked
from v1_execution_walk_forward import ExecutionConfig


class CashReleaseSensitivityTests(unittest.TestCase):
    def frame(self):
        rows = []
        for i, outcome in enumerate((0.0, 0.0)):
            rows.append({
                "first_quote_ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * i),
                "run_name": "run-1",
                "market_id": f"m-{i}",
                "classic_early_drop_down_mean_positive_side": "buy_down",
                "classic_early_drop_down_mean_positive_minute": 1,
                "classic_early_drop_down_mean_positive_fraction": 0.50,
                "outcome_up": outcome,
                "exec_buy_down_1m_d4_quote_price": 0.50,
                "exec_buy_down_1m_d4_signal_price": 0.50,
                "exec_buy_down_1m_d4_quote_size": 200.0,
                "exec_buy_down_1m_d4_quote_spread": 0.01,
                "exec_buy_down_1m_d4_quote_overround": 0.01,
                "exec_buy_down_1m_d4_adverse_slippage": 0.0,
                "exec_buy_up_1m_d4_quote_price": float("nan"),
                "exec_buy_up_1m_d4_signal_price": float("nan"),
                "exec_buy_up_1m_d4_quote_size": float("nan"),
                "exec_buy_up_1m_d4_quote_spread": float("nan"),
                "exec_buy_up_1m_d4_quote_overround": float("nan"),
                "exec_buy_up_1m_d4_adverse_slippage": float("nan"),
            })
        return pd.DataFrame(rows)

    def test_cash_cannot_be_reused_before_release(self):
        immediate, immediate_metrics = simulate_cash_locked(
            self.frame(), "classic_early_drop_down_mean_positive", ExecutionConfig(), 10.0, 5.0, 0.0
        )
        delayed, delayed_metrics = simulate_cash_locked(
            self.frame(), "classic_early_drop_down_mean_positive", ExecutionConfig(), 10.0, 5.0, 600.0
        )
        self.assertEqual(immediate_metrics["trades"], 2)
        self.assertEqual(delayed_metrics["trades"], 1)
        self.assertEqual(len(immediate), 2)
        self.assertEqual(len(delayed), 1)

    def test_execution_envelope_can_block_insufficient_depth(self):
        _, metrics = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(min_depth=300.0),
            10.0,
            5.0,
            600.0,
        )
        self.assertEqual(metrics["trades"], 0)

    def test_maker_contract_has_zero_execution_fee(self):
        _, metrics = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
        )
        self.assertEqual(metrics["fee_mode"], "polymarket_taker")
        self.assertEqual(metrics["fee_rate"], 0.0)
        self.assertEqual(metrics["trades"], 2)

    def test_share_floor_is_explicit_and_can_consume_the_small_ledger(self):
        _, metrics = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            min_order_shares=19.0,
        )
        self.assertEqual(metrics["min_order_notional"], 5.0)
        self.assertEqual(metrics["min_order_shares"], 19.0)
        self.assertEqual(metrics["trades"], 1)


if __name__ == "__main__":
    unittest.main()
