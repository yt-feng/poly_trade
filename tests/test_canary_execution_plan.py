from decimal import Decimal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from canary_execution_plan import build_plan, minimum_gross_buy_shares, net_buy_shares_conservative


class CanaryExecutionPlanTests(unittest.TestCase):
    def test_half_price_needs_more_than_five_gross_shares(self):
        gross = minimum_gross_buy_shares("5", "0.50", "0.07")
        self.assertEqual(gross, Decimal("5.19"))
        self.assertGreaterEqual(net_buy_shares_conservative(gross, "0.50", "0.07"), Decimal("5"))

    def test_plan_is_never_live(self):
        p = build_plan(entry_price="0.50", tick_size="0.01", min_order_size="5", fee_rate="0.07")
        self.assertTrue(p["eligible_for_execution_validation_plan"])
        self.assertFalse(p["live_enabled"])
        self.assertFalse(p["orders_submitted"])
        self.assertEqual(p["exit_min_price_on_current_tick"], "0.98")

    def test_low_cap_blocks_reversible_minimum(self):
        p = build_plan(entry_price="0.50", tick_size="0.01", min_order_size="5", fee_rate="0.07", notional_cap_usd="1")
        self.assertIn("blocked_minimum_reversible_size_over_notional_cap", p["block_reasons"])

    def test_dynamic_tick_is_enforced(self):
        p = build_plan(entry_price="0.505", tick_size="0.01", min_order_size="5", fee_rate="0.07")
        self.assertIn("blocked_entry_price_off_tick", p["block_reasons"])

    def test_098_semantics_fail_closed_when_tick_cannot_represent_it(self):
        p = build_plan(entry_price="0.5", tick_size="0.1", min_order_size="5", fee_rate="0.07")
        self.assertIn("blocked_098_not_representable_at_current_tick", p["block_reasons"])

    def test_unknown_tick_fails_closed(self):
        p = build_plan(entry_price="0.50", tick_size="0.03", min_order_size="5", fee_rate="0.07")
        self.assertIn("blocked_tick_unknown", p["block_reasons"])

    def test_extreme_price_still_preserves_net_minimum(self):
        p = build_plan(entry_price="0.99", tick_size="0.01", min_order_size="5", fee_rate="0.07")
        self.assertTrue(p["eligible_for_execution_validation_plan"])
        self.assertGreaterEqual(Decimal(p["conservative_net_shares"]), Decimal("5"))


if __name__ == "__main__":
    unittest.main()
