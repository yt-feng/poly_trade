import sys,unittest
from pathlib import Path
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from canary_execution_plan import audit_exit_payoff,build_plan,fee_usdc_conservative

def profile(**kw):
    x=dict(entry_cash_cost='1.063',sellable_shares='10',exit_bid='.15',tick_size='.01',exit_fee_rate='.07')
    x.update(kw);return audit_exit_payoff(**x)
class ExitPayoffTests(unittest.TestCase):
    def test_price_up_fifty_is_not_net_fifty(self):
        p=profile();self.assertLess(Decimal(p['net_roi_if_exit_occurs']),Decimal('.5'));self.assertFalse(p['target_met_at_supplied_bid'])
    def test_exact_net_target_minimal(self):
        p=profile();bid=Decimal(p['minimum_bid_for_desired_roi']);self.assertEqual(bid,Decimal('.17'))
        self.assertGreaterEqual(10*bid-fee_usdc_conservative(10,bid,'.07'),Decimal('1.063')*Decimal('1.5'))
        self.assertLess(10*(bid-Decimal('.01'))-fee_usdc_conservative(10,bid-Decimal('.01'),'.07'),Decimal('1.063')*Decimal('1.5'))
    def test_break_even_includes_both_fees(self):self.assertEqual(profile()['break_even_bid_on_supplied_grid'],'0.12')
    def test_high_entry_unreachable(self):
        p=profile(entry_cash_cost='9.8',exit_bid='.98');self.assertIsNone(p['minimum_bid_for_desired_roi'])
    def test_same_entry_exit_loses_fees(self):self.assertLess(Decimal(profile(exit_bid='.10')['net_pnl_if_exit_occurs']),0)
    def test_no_expectation_or_cash_claim(self):
        p=profile();self.assertFalse(p['cash_actually_received_verified']);self.assertFalse(p['orders_submitted']);self.assertFalse(p['changes_exit_policy'])
    def test_more_sellable_shares_changes_price_floor(self):self.assertLess(Decimal(profile(sellable_shares='20')['minimum_bid_for_desired_roi']),Decimal(profile()['minimum_bid_for_desired_roi']))
    def test_unsupported_grid(self):self.assertRaises(ValueError,profile,tick_size='.03')
    def test_zero_grid(self):self.assertRaises(ValueError,profile,tick_size='0')
    def test_off_grid_bid(self):self.assertRaises(ValueError,profile,exit_bid='.155')
    def test_non_finite(self):self.assertRaises(ValueError,profile,entry_cash_cost='NaN')
    def test_no_negative_target(self):self.assertRaises(ValueError,profile,desired_net_roi='-.1')
    def test_bool_rejected(self):self.assertRaises(ValueError,profile,entry_cash_cost=True)
    def test_no_zero_shares(self):self.assertRaises(ValueError,profile,sellable_shares='0')
    def test_no_zero_cost(self):self.assertRaises(ValueError,profile,entry_cash_cost='0')
    def test_rate_unknown(self):self.assertRaises(ValueError,profile,exit_fee_rate=None)
    def test_098_preserved_and_new_field_called(self):
        p=build_plan(entry_price='.5',tick_size='.01',min_order_size='5',fee_rate='.07',notional_cap_usd='6',min_buy_notional_usd='1')
        self.assertEqual(p['exit_trigger_best_bid'],'0.98');self.assertEqual(p['all_in_cost_usd_equivalent'],'2.58750');self.assertIsNotNone(p['hypothetical_exit_payoff']);self.assertFalse(p['live_enabled'])
    def test_blocked_plan_does_not_compute_profit(self):
        p=build_plan(entry_price='.5',tick_size='.01',min_order_size='13',fee_rate='.07',notional_cap_usd='6',min_buy_notional_usd='1');self.assertIsNone(p['hypothetical_exit_payoff'])
    def test_zero_target_is_break_even(self):
        p=profile(desired_net_roi='0');self.assertEqual(p['minimum_bid_for_desired_roi'],p['break_even_bid_on_supplied_grid'])
    def test_smaller_grid_does_not_inflate_target(self):self.assertLessEqual(Decimal(profile(tick_size='.001')['minimum_bid_for_desired_roi']),Decimal(profile()['minimum_bid_for_desired_roi']))
if __name__=='__main__':unittest.main()
