"""Synthetic classification regressions; no account, network or order API."""
from pathlib import Path
import sys,unittest,copy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from execution_reason_codes import classify,summarize

def fixture(**extra):
    r=dict(signal_qualified=True,planned_shares='5',min_order_size='5',requested_shares='5',confirmed_filled_shares='0',entry_order_terminal=False,order_terminal=False,order_status='')
    r.update(extra);return r
class LifecyclePrecedence(unittest.TestCase):
    def code(self,**kw):return classify(fixture(**kw))['reason_code']
    def test_no_signal_flat(self):self.assertEqual(self.code(signal_qualified=False),'NO_QUALIFIED_OPPORTUNITY')
    def test_missing_signal_unknown(self):self.assertEqual(classify({})['reason_code'],'SIGNAL_QUALIFICATION_UNKNOWN')
    def test_string_signal_unknown(self):self.assertEqual(self.code(signal_qualified='false'),'SIGNAL_QUALIFICATION_UNKNOWN')
    def test_unknown_overrides_no_signal(self):self.assertEqual(self.code(signal_qualified=False,reconciliation_unknown=True),'RECONCILIATION_UNKNOWN')
    def test_expired_overrides_no_signal(self):self.assertEqual(self.code(signal_qualified=False,confirmed_filled_shares='5',entry_order_terminal=True,market_expired=True),'EXPIRED_AWAITING_OFFICIAL_RESOLUTION')
    def test_redeemable_overrides_no_signal(self):self.assertEqual(self.code(signal_qualified=False,confirmed_filled_shares='5',entry_order_terminal=True,market_expired=True,official_resolution_observed=True,redeemable=True),'REDEEMABLE_NOT_REQUESTED')
    def test_partial_reject_inventory_not_rejected(self):self.assertEqual(self.code(confirmed_filled_shares='2',order_terminal=True,order_status='REJECTED'),'PARTIAL_CONFIRMED_FILL')
    def test_partial_terminal_episode_not_final(self):self.assertFalse(classify(fixture(confirmed_filled_shares='2',order_terminal=True))['final_for_episode'])
    def test_partial_expiry(self):self.assertEqual(self.code(confirmed_filled_shares='2',order_terminal=True,market_expired=True),'EXPIRED_AWAITING_OFFICIAL_RESOLUTION')
    def test_stale_quote_does_not_hide_inventory(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,quote_age_ms=9000,max_quote_age_ms=1000),'CONFIRMED_ENTRY_POSITION_OPEN')
    def test_changed_minimum_does_not_hide_inventory(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,min_order_size='6'),'CONFIRMED_ENTRY_POSITION_OPEN')
    def test_old_low_cash_does_not_hide_inventory(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,planned_all_in_usd=10,total_cash_usd=1),'CONFIRMED_ENTRY_POSITION_OPEN')
    def test_old_slow_compute_does_not_hide_inventory(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,decision_to_submit_ms=9000,max_decision_to_submit_ms=100),'CONFIRMED_ENTRY_POSITION_OPEN')
    def test_cash_pending_no_signal(self):self.assertEqual(self.code(signal_qualified=False,confirmed_filled_shares='5',entry_order_terminal=True,exit_trade_confirmed=True),'EXIT_CONFIRMED_CASH_CHECKPOINT_PENDING')
    def test_remaining_after_partial_exit_not_cash(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,exit_trade_confirmed=True,remaining_position_shares='2'),'CONFIRMED_ENTRY_POSITION_OPEN')
    def test_flat_conflicts_remaining(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,position_flat=True,remaining_position_shares='2'),'POSITION_EVIDENCE_CONFLICT')
    def test_cash_complete_no_signal(self):self.assertEqual(self.code(signal_qualified=False,confirmed_filled_shares='5',entry_order_terminal=True,cash_reconciled=True,position_flat=True),'CASH_RECONCILED_ROUNDTRIP_COMPLETE')
    def test_unknown_overrides_complete(self):self.assertEqual(self.code(confirmed_filled_shares='5',entry_order_terminal=True,cash_reconciled=True,position_flat=True,reconciliation_unknown=True),'RECONCILIATION_UNKNOWN')
    def test_cash_without_entry_not_certified(self):self.assertEqual(self.code(cash_reconciled=True,position_flat=True),'POSITION_OR_CASH_EVIDENCE_INCOMPLETE')
    def test_submitted_pending_no_signal(self):self.assertEqual(self.code(signal_qualified=False,order_submitted=True,order_status='MATCHED'),'ORDER_ACCEPTED_NOT_CONFIRMED_FILLED')
    def test_submitted_terminal(self):self.assertEqual(self.code(signal_qualified=False,order_submitted=True,order_terminal=True),'ORDER_TERMINAL_UNFILLED')
    def test_submitted_rejected(self):self.assertEqual(self.code(order_submitted=True,order_status='REJECTED'),'ORDER_REJECTED')
    def test_negative_confirmed(self):self.assertRaises(ValueError,classify,fixture(confirmed_filled_shares='-1'))
    def test_confirmed_above_request(self):self.assertRaises(ValueError,classify,fixture(confirmed_filled_shares='6'))
    def test_true_minimum_failure_preserved(self):self.assertEqual(self.code(planned_shares='4'),'ALGO_SIZE_BELOW_EXCHANGE_MINIMUM')
    def test_true_stale_quote_preserved(self):self.assertEqual(self.code(quote_age_ms=9000,max_quote_age_ms=1000),'QUOTE_STALE_OR_CLOCK_INVALID')
    def test_no_mutation(self):
        r=fixture(confirmed_filled_shares='2',order_terminal=True);old=copy.deepcopy(r);classify(r);self.assertEqual(r,old)
    def test_summary_no_closed_inventory(self):
        s=summarize([fixture(confirmed_filled_shares='2',order_terminal=True),fixture(signal_qualified=False,reconciliation_unknown=True)]);self.assertEqual(s['final_episodes'],0)
    def test_no_execution(self):self.assertFalse(classify(fixture(confirmed_filled_shares='5'))['live_action_taken'])
if __name__=='__main__':unittest.main()
