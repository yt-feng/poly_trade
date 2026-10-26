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

    def test_declared_free_cash_buffer_blocks_entries_below_buffer(self):
        _, no_buffer = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            minimum_free_cash_after_entry=0.0,
        )
        _, buffered = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            minimum_free_cash_after_entry=5.0,
        )
        self.assertEqual(no_buffer["trades"], 2)
        self.assertEqual(buffered["trades"], 1)
        self.assertEqual(buffered["minimum_free_cash_after_entry"], 5.0)

    def test_single_position_gate_waits_for_cash_release(self):
        _, metrics = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            single_position_only=True,
        )
        self.assertEqual(metrics["trades"], 1)
        self.assertEqual(metrics["blocked_single_position_events"], 1)
        self.assertTrue(metrics["single_position_only"])

    def test_execution_clock_uses_fill_quote_time_for_serial_release(self):
        frame = self.frame()
        frame.loc[0, "first_quote_ts"] = pd.Timestamp("2026-01-01T00:00:00Z")
        frame.loc[1, "first_quote_ts"] = pd.Timestamp("2026-01-01T00:15:50Z")
        frame["exec_buy_down_1m_d4_quote_ts"] = [
            pd.Timestamp("2026-01-01T00:02:00Z"),
            pd.Timestamp("2026-01-01T00:15:51Z"),
        ]
        first_clock, first_metrics = simulate_cash_locked(
            frame,
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            single_position_only=True,
        )
        execution_clock, execution_metrics = simulate_cash_locked(
            frame,
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            single_position_only=True,
            entry_clock="execution_quote_ts",
        )
        self.assertEqual(first_metrics["trades"], 2)
        self.assertEqual(execution_metrics["trades"], 1)
        self.assertEqual(execution_metrics["entry_clock"], "execution_quote_ts")
        self.assertEqual(execution_metrics["blocked_single_position_events"], 1)
        self.assertEqual(len(first_clock), 2)
        self.assertEqual(len(execution_clock), 1)

    def test_execution_clock_rejects_missing_quote_timestamp(self):
        frame = self.frame()
        frame["exec_buy_down_1m_d4_quote_ts"] = [pd.Timestamp("2026-01-01T00:02:00Z"), pd.NaT]
        _, metrics = simulate_cash_locked(
            frame,
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            0.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            entry_clock="execution_quote_ts",
        )
        self.assertEqual(metrics["trades"], 1)
        self.assertEqual(metrics["blocked_entry_clock_events"], 1)

    def test_relative_capacity_gate_uses_fee_aware_minimum_cash(self):
        _, metrics = simulate_cash_locked(
            self.frame(),
            "classic_early_drop_down_mean_positive",
            ExecutionConfig(),
            10.0,
            5.0,
            600.0,
            fee_mode="polymarket_taker",
            fee_rate=0.0,
            minimum_capacity_ratio=6.0,
        )
        self.assertEqual(metrics["trades"], 0)
        self.assertEqual(metrics["blocked_capacity_ratio_events"], 2)
        self.assertEqual(metrics["minimum_capacity_ratio"], 6.0)


if __name__ == "__main__":
    unittest.main()
