"""Offline supplied-rule arithmetic; never calls or authenticates an exchange."""
import sys, unittest
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from canary_execution_plan import build_plan,minimum_gross_buy_shares,SHARE_FEE_SENSITIVITY

def plan(**kw):
 p=dict(entry_price='.05',tick_size='.01',min_order_size='5',fee_rate='.07',notional_cap_usd='6')
 p.update(kw);return build_plan(**p)

class MinimumNotionalTests(unittest.TestCase):
 def test_legacy_is_not_a_verified_notional_check(self):
  p=plan();self.assertEqual(p['gross_buy_shares'],'5');self.assertEqual(p['minimum_notional_status'],'not_supplied_not_checked');self.assertFalse(p['supplied_minima_satisfied'])
 def test_penny_buy_needs_twenty_shares(self):
  p=plan(min_buy_notional_usd='1');self.assertEqual(D(p['gross_buy_shares']),20);self.assertEqual(D(p['all_in_cost_usd_equivalent']),D('1.06650'))
 def test_midprice_five_unchanged(self):
  p=plan(entry_price='.50',min_buy_notional_usd='1');self.assertEqual(D(p['gross_buy_shares']),5);self.assertEqual(D(p['all_in_cost_usd_equivalent']),D('2.58750'))
 def test_six_share_minimum(self):
  p=plan(entry_price='.5',min_order_size=6,min_buy_notional_usd=1);self.assertEqual(D(p['gross_buy_shares']),6)
 def test_amount_floor_rounds_up(self):
  p=plan(entry_price='.07',min_buy_notional_usd=1);self.assertEqual(D(p['gross_buy_shares']),D('14.29'));self.assertGreaterEqual(D(p['gross_notional_usd']),1)
 def test_exact_floor(self):self.assertEqual(D(plan(entry_price='.2',min_buy_notional_usd=1)['gross_buy_shares']),5)
 def test_fee_cannot_count_towards_floor(self):self.assertGreaterEqual(D(plan(min_buy_notional_usd=1)['gross_notional_usd']),1)
 def test_cash_budget_includes_fee(self):
  p=plan(min_buy_notional_usd=1,notional_cap_usd=1);self.assertFalse(p['eligible_for_execution_validation_plan']);self.assertIn('blocked_minimum_reversible_size_over_notional_cap',p['block_reasons'])
 def test_floor_over_cap_blocks_without_huge_search(self):self.assertIn('blocked_buy_notional_minimum_over_cap',plan(min_buy_notional_usd=10000000)['block_reasons'])
 def test_zero_invalid(self):self.assertIn('blocked_buy_notional_minimum_invalid',plan(min_buy_notional_usd=0)['block_reasons'])
 def test_negative_invalid(self):self.assertFalse(plan(min_buy_notional_usd=-1)['supplied_minima_satisfied'])
 def test_nan_invalid(self):self.assertFalse(plan(min_buy_notional_usd='NaN')['supplied_minima_satisfied'])
 def test_inf_invalid(self):self.assertFalse(plan(min_buy_notional_usd='Infinity')['supplied_minima_satisfied'])
 def test_boolean_invalid(self):self.assertFalse(plan(min_buy_notional_usd=True)['supplied_minima_satisfied'])
 def test_source_is_not_authentication(self):
  p=plan(min_buy_notional_usd=1,min_buy_notional_source='fixture');self.assertEqual(p['minimum_buy_notional_source'],'fixture');self.assertFalse(p['exchange_rule_provenance_verified'])
 def test_no_orders(self):
  p=plan(min_buy_notional_usd=1);self.assertFalse(p['orders_submitted']);self.assertFalse(p['live_enabled'])
 def test_tp098_retained(self):self.assertEqual(plan(min_buy_notional_usd=1)['exit_trigger_best_bid'],'0.98')
 def test_share_deduction_scenario_keeps_net_exitable(self):
  p=plan(entry_price='.5',min_buy_notional_usd=1,fee_asset_scenario=SHARE_FEE_SENSITIVITY);self.assertGreaterEqual(D(p['conservative_net_shares']),5)
 def test_direct_minimum_function(self):self.assertEqual(minimum_gross_buy_shares(5,'.05','.07',min_buy_notional_usd=1),D('20'))
 def test_invalid_tick_preserves_prior_fix(self):self.assertFalse(plan(tick_size=0,min_buy_notional_usd=1)['eligible_for_execution_validation_plan'])

if __name__=='__main__':unittest.main()
