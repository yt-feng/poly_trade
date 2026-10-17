import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from live_execution import LiveExecutionError, LiveExecutionSettings, build_buy_market_plan, price_text


class LiveExecutionPlanTests(unittest.TestCase):
    def settings(self):
        return LiveExecutionSettings("", "", "", Decimal("6"), "FAK")

    def test_price_uses_current_tick(self):
        self.assertEqual(price_text("0.506", "0.005"), "0.505")
        self.assertEqual(price_text("0.506", "0.01"), "0.50")

    def test_invalid_tick_is_blocked(self):
        with self.assertRaises(LiveExecutionError):
            price_text("0.5", "0")

    def test_plan_records_fee_and_net_share_boundary(self):
        plan = build_buy_market_plan(
            local_order_id="o1", slug="s", side="buy_up", token_id="t",
            intended_cost=5.0, signal_price=0.5, max_adverse_slippage=0.01,
            min_order_size=12.0, tick_size=0.01, fee_rate=0.07, settings=self.settings(),
        )
        self.assertEqual(plan.max_price, "0.51")
        self.assertEqual(plan.net_shares_after_fee, plan.estimated_shares)
        self.assertEqual(plan.tick_size, "0.01")
        self.assertEqual(plan.blocked_reason, "blocked_min_order_size")


if __name__ == "__main__":
    unittest.main()
