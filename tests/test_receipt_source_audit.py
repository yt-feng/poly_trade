from pathlib import Path
import sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from receipt_source_audit import audit
from canary_receipt_state import (
    apply_event,
    available_sell_shares,
    completed_roundtrip_count,
    new_episode,
    reserved_sell_shares,
)
class Audit(unittest.TestCase):
    def fixture(self,runner,adapter='def price_text(p):\n return round(p,2)\n'):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);root=Path(t.name);(root/'analysis').mkdir()
        (root/'analysis/live_paper_trader.py').write_text(runner);(root/'analysis/live_execution.py').write_text(adapter);return root
    def test_unconditional_mark(self):
        r=audit(self.fixture('def record_signal(self):\n self.mark_slug_traded("x")\n'))
        self.assertIn('ATTEMPT_MARKED_AS_TRADED',[f['code']for f in r['findings']])
    def test_guarded_mark_not_same_pattern(self):
        r=audit(self.fixture('def record_signal(self):\n if self.confirmed:\n  self.mark_slug_traded("x")\n'))
        self.assertNotIn('ATTEMPT_MARKED_AS_TRADED',[f['code']for f in r['findings']])
    def test_no_execution(self):
        r=audit(self.fixture('raise RuntimeError("NEVER EXECUTE INPUT")\ndef settle_positions(self):\n self.bankroll=self.last_final_by_slug\n'))
        self.assertFalse(r['execution_enabled']);self.assertFalse(r['account_cash_verified'])
    def test_tick_pattern(self):
        r=audit(self.fixture('x=0\n','def price_text(p):\n return p.quantize(Decimal("0.01"))\n'))
        self.assertIn('HARDCODED_PRICE_GRID',[f['code']for f in r['findings']])
    def test_pending_identity(self):
        r=audit(self.fixture('def process_pending(self,snap):\n return snap["price"]\n'))
        self.assertIn('NO_PENDING_SNAPSHOT_MARKET_CHECK',[f['code']for f in r['findings']])
    def test_guarded_pending(self):
        r=audit(self.fixture('def process_pending(self,snap):\n if snap["slug"] != self.slug: return\n'))
        self.assertNotIn('NO_PENDING_SNAPSHOT_MARKET_CHECK',[f['code']for f in r['findings']])
    def test_no_sell_not_certification(self):
        r=audit(self.fixture('x=0\n','def sell_placeholder(): pass\n'))
        self.assertFalse(r['live_098_implementation_verified'])
    def test_buy_fee_net_shares_exit_minimum(self):
        adapter='def build_buy_market_plan(min_order_size):\n estimated_shares=5\n if estimated_shares < min_order_size: return "blocked"\n return estimated_shares\n'
        r=audit(self.fixture('x=0\n',adapter))
        self.assertIn('BUY_FEE_NET_SHARES_EXIT_MIN_UNCHECKED',[f['code']for f in r['findings']])


class ReceiptCashContract(unittest.TestCase):
    def base(self):
        return new_episode(local_id="ep-1", token_id="token", min_sell_size="5")

    def opened(self):
        s=self.base()
        s=apply_event(s,{"event_id":"a1","kind":"entry_order_accepted","order_id":"buy-1"})
        s=apply_event(s,{"event_id":"f1","kind":"entry_fill","trade_id":"trade-in-1","gross_filled_shares":"5.4","net_position_delta_shares":"5.2"})
        s=apply_event(s,{"event_id":"t1","kind":"entry_order_terminal"})
        return s

    def test_acceptance_is_not_fill_or_completed_trade(self):
        s=apply_event(self.base(),{"event_id":"a1","kind":"entry_order_accepted","order_id":"buy-1"})
        self.assertEqual(s["state"],"entry_accepted")
        self.assertEqual(s["entry_trade_ids"],[])
        self.assertFalse(s["completed_roundtrip"])
        self.assertFalse(s["cash_reusable"])

    def test_rejected_order_does_not_count(self):
        s=apply_event(self.base(),{"event_id":"r1","kind":"entry_order_rejected"})
        self.assertEqual(completed_roundtrip_count([s]),0)
        self.assertEqual(s["entry_trade_ids"],[])

    def test_split_entry_fills_same_episode_count_once(self):
        s=self.base()
        s=apply_event(s,{"event_id":"a1","kind":"entry_order_accepted","order_id":"buy-1"})
        s=apply_event(s,{"event_id":"f1","kind":"entry_fill","trade_id":"in-1","gross_filled_shares":"2.7","net_position_delta_shares":"2.6"})
        s=apply_event(s,{"event_id":"f2","kind":"entry_fill","trade_id":"in-2","gross_filled_shares":"2.7","net_position_delta_shares":"2.6"})
        s=apply_event(s,{"event_id":"t1","kind":"entry_order_terminal"})
        self.assertEqual(s["position_net_shares"],"5.2")
        self.assertEqual(len(s["entry_trade_ids"]),2)
        self.assertFalse(s["completed_roundtrip"])
        self.assertEqual(completed_roundtrip_count([s]),0)

    def test_terminal_subminimum_entry_halts_without_auto_topup(self):
        s=self.base()
        s=apply_event(s,{"event_id":"f1","kind":"entry_fill","trade_id":"in-1","gross_filled_shares":"4","net_position_delta_shares":"3.9"})
        s=apply_event(s,{"event_id":"t1","kind":"entry_order_terminal"})
        self.assertTrue(s["halt_new_actions"])
        self.assertEqual(s["halt_reason"],"matched_entry_net_shares_below_current_sell_minimum")

    def test_098_trigger_uses_best_bid_not_ask_or_mid(self):
        s=self.opened()
        s=apply_event(s,{"event_id":"q1","kind":"quote","best_bid":"0.97","best_ask":"0.99","mid":"0.98"})
        self.assertFalse(s["exit_intent_ready"])
        s=apply_event(s,{"event_id":"q2","kind":"quote","best_bid":"0.98","best_ask":"0.99","mid":"0.985"})
        self.assertTrue(s["exit_intent_ready"])
        self.assertEqual(s["state"],"exit_intent_ready")

    def test_sell_reservation_survives_cancel_request_until_confirmation(self):
        s=self.opened()
        s=apply_event(s,{"event_id":"q","kind":"quote","best_bid":"0.98"})
        s=apply_event(s,{"event_id":"sa","kind":"sell_order_accepted","order_id":"sell-1","reserved_shares":"5.2"})
        self.assertEqual(str(reserved_sell_shares(s)),"5.2")
        self.assertEqual(str(available_sell_shares(s)),"0")
        s=apply_event(s,{"event_id":"cr","kind":"sell_cancel_requested","order_id":"sell-1"})
        self.assertEqual(str(reserved_sell_shares(s)),"5.2")
        s=apply_event(s,{"event_id":"cc","kind":"sell_cancel_confirmed","order_id":"sell-1"})
        self.assertEqual(str(reserved_sell_shares(s)),"0")
        self.assertEqual(str(available_sell_shares(s)),"5.2")

    def test_partial_sell_then_cancel_can_strand_subminimum_position(self):
        s=self.opened()
        s=apply_event(s,{"event_id":"q","kind":"quote","best_bid":"0.98"})
        s=apply_event(s,{"event_id":"sa","kind":"sell_order_accepted","order_id":"sell-1","reserved_shares":"5.2"})
        s=apply_event(s,{"event_id":"sf","kind":"sell_fill","order_id":"sell-1","trade_id":"out-1","filled_shares":"2","reported_proceeds_usd":"1.96"})
        self.assertFalse(s["halt_new_actions"])  # 3.2 shares remain reserved on the accepted order.
        s=apply_event(s,{"event_id":"cr","kind":"sell_cancel_requested","order_id":"sell-1"})
        self.assertFalse(s["halt_new_actions"])
        s=apply_event(s,{"event_id":"cc","kind":"sell_cancel_confirmed","order_id":"sell-1"})
        self.assertTrue(s["halt_new_actions"])
        self.assertEqual(s["halt_reason"],"remaining_position_below_current_sell_minimum")
        self.assertEqual(s["position_net_shares"],"3.2")

    def test_full_sell_still_waits_for_cash_reconciliation(self):
        s=self.opened()
        s=apply_event(s,{"event_id":"q","kind":"quote","best_bid":"0.98"})
        s=apply_event(s,{"event_id":"sa","kind":"sell_order_accepted","order_id":"sell-1","reserved_shares":"5.2"})
        s=apply_event(s,{"event_id":"sf","kind":"sell_fill","order_id":"sell-1","trade_id":"out-1","filled_shares":"5.2","reported_proceeds_usd":"5.096"})
        self.assertEqual(s["position_net_shares"],"0.0")
        self.assertFalse(s["cash_reusable"])
        self.assertFalse(s["completed_roundtrip"])
        s=apply_event(s,{"event_id":"cash","kind":"cash_reconciled","available_cash_usd":"10.096"})
        self.assertTrue(s["cash_reusable"])
        self.assertTrue(s["completed_roundtrip"])
        self.assertEqual(completed_roundtrip_count([s]),1)

    def test_expiry_redeem_does_not_credit_cash_before_reconcile(self):
        s=self.opened()
        s=apply_event(s,{"event_id":"exp","kind":"market_expired"})
        self.assertEqual(s["state"],"settlement_pending")
        s=apply_event(s,{"event_id":"ready","kind":"settlement_redeemable"})
        s=apply_event(s,{"event_id":"req","kind":"redeem_requested"})
        s=apply_event(s,{"event_id":"rec","kind":"redeem_confirmed","receipt_id":"redeem-1"})
        self.assertFalse(s["cash_reusable"])
        self.assertFalse(s["completed_roundtrip"])
        s=apply_event(s,{"event_id":"cash","kind":"cash_reconciled","available_cash_usd":"9.5"})
        self.assertTrue(s["completed_roundtrip"])

    def test_duplicate_receipt_is_idempotent(self):
        s=self.opened()
        e={"event_id":"q","kind":"quote","best_bid":"0.98"}
        once=apply_event(s,e)
        twice=apply_event(once,e)
        self.assertEqual(once,twice)

    def test_unknown_reconciliation_halts_new_actions(self):
        s=self.opened()
        s=apply_event(s,{"event_id":"u","kind":"reconciliation_unknown","reason":"receipt_timeout"})
        self.assertTrue(s["halt_new_actions"])
        self.assertFalse(s["cash_reusable"])
        self.assertEqual(s["state"],"unknown_reconciliation")

if __name__=='__main__':unittest.main()
