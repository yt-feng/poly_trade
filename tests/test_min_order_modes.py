import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from v1_execution_walk_forward import ExecutionConfig, simulate


def one_signal(fraction=0.10):
    row = {
        "first_quote_ts": pd.Timestamp("2026-01-01T00:00:00Z"),
        "run_name": "synthetic",
        "market_id": "synthetic::one",
        "outcome_up": 1.0,
        "v1_conservative_mix_side": "buy_up",
        "v1_conservative_mix_minute": 1,
        "v1_conservative_mix_fraction": fraction,
        "v1_conservative_mix_component": "test",
        "exec_buy_up_1m_d4_quote_price": 0.10,
        "exec_buy_up_1m_d4_signal_price": 0.10,
        "exec_buy_up_1m_d4_quote_size": 1000.0,
        "exec_buy_up_1m_d4_quote_spread": 0.01,
        "exec_buy_up_1m_d4_quote_overround": 0.01,
        "exec_buy_up_1m_d4_adverse_slippage": 0.0,
    }
    for side in ("buy_up", "buy_down"):
        for minute in (1, 2, 4):
            prefix = f"exec_{side}_{minute}m_d4"
            for field in ("quote_price", "signal_price", "quote_size",
                          "quote_spread", "quote_overround", "adverse_slippage"):
                row.setdefault(f"{prefix}_{field}", float("nan"))
    return pd.DataFrame([row])


class MinimumOrderModeTests(unittest.TestCase):
    def run_one(self, mode, min_cost=0.0, policy="raise_to_min", fraction=0.10):
        return simulate(
            one_signal(fraction), "v1_conservative_mix", ExecutionConfig(), 10.0,
            min_cost, capture_logs=True, fee_mode="polymarket_taker",
            fee_rate=0.07, min_cost_policy=policy, min_order_mode=mode,
            min_order_shares=5.0,
        )

    def test_shares_mode_raises_to_five_shares(self):
        logs, metrics = self.run_one("shares", fraction=0.01)
        self.assertEqual(metrics["trades"], 1)
        self.assertAlmostEqual(float(logs.iloc[0]["filled_cost"]), 0.5125, places=6)

    def test_notional_mode_raises_to_five_dollars(self):
        logs, metrics = self.run_one("notional", min_cost=5.0)
        self.assertEqual(metrics["trades"], 1)
        self.assertAlmostEqual(float(logs.iloc[0]["filled_cost"]), 5.0, places=6)

    def test_both_mode_requires_both_floors(self):
        logs, metrics = self.run_one("both", min_cost=5.0)
        self.assertEqual(metrics["trades"], 1)
        self.assertAlmostEqual(float(logs.iloc[0]["filled_cost"]), 5.0, places=6)

    def test_shares_reject_policy_does_not_raise_size(self):
        logs, metrics = self.run_one("shares", policy="reject", fraction=0.01)
        self.assertEqual(metrics["trades"], 0)
        self.assertTrue(logs.empty)

    def test_one_continuous_call_preserves_trade_window_across_runs(self):
        frames = []
        for index in range(11):
            frame = one_signal(fraction=0.01).copy()
            frame["first_quote_ts"] = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(seconds=index)
            frame["run_name"] = f"run-{index}"
            frame["market_id"] = f"market-{index}"
            frames.append(frame)
        combined = pd.concat(frames, ignore_index=True)
        _, global_metrics = simulate(
            combined, "v1_conservative_mix", ExecutionConfig(), 10.0, 0.0,
            capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07,
            min_cost_policy="raise_to_min", min_order_mode="notional",
            min_order_shares=5.0,
        )
        separate_trades = 0
        for frame in frames:
            _, metrics = simulate(
                frame, "v1_conservative_mix", ExecutionConfig(), 10.0, 0.0,
                capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07,
                min_cost_policy="raise_to_min", min_order_mode="notional",
                min_order_shares=5.0,
            )
            separate_trades += metrics["trades"]
        self.assertEqual(global_metrics["trades"], 10)
        self.assertEqual(separate_trades, 11)


if __name__ == "__main__":
    unittest.main()
