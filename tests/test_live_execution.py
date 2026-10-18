import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from live_execution import (
    LiveExecutionError,
    LiveExecutionSettings,
    build_buy_market_plan,
    build_redeem_plan,
    build_sell_limit_plan,
    price_text,
)


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

    def test_sell_plan_is_tick_aware_and_plan_only(self):
        plan = build_sell_limit_plan(
            local_order_id="s1", slug="s", side="buy_up", token_id="t",
            shares=5.0, limit_price=0.987, tick_size=0.005, min_order_size=5.0,
        )
        self.assertEqual(plan.limit_price, "0.985")
        self.assertEqual(plan.event_fields()["orders_submitted"], False)

    def test_redeem_plan_requires_official_resolution_reference(self):
        blocked = build_redeem_plan(
            local_redeem_id="r1", condition_id="c", token_ids=["u", "d"], shares=5,
        )
        self.assertEqual(blocked.blocked_reason, "blocked_missing_official_resolution_receipt")
        ready = build_redeem_plan(
            local_redeem_id="r2", condition_id="c", token_ids=["u", "d"], shares=5,
            official_resolution_ref="tx-resolution",
        )
        self.assertEqual(ready.blocked_reason, "")
        self.assertFalse(ready.event_fields()["orders_submitted"])


if __name__ == "__main__":
    unittest.main()
