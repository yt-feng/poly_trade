from pathlib import Path
import copy, sys, unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"analysis"),
              str(Path(__file__).resolve().parents[1]/"reference")]
from canary_receipt_amount_state import apply_event
from test_cash_amount_audit import fixtures, exited

def complete():
    a,b,c=fixtures()
    c.update(opening_cash_checkpoint=a, cash_flow_evidence=b)
    return apply_event(exited(apply_event), c)

def late(s):
    events=[
      dict(event_id="late-in",kind="entry_fill",trade_id="new-in",trade_status="CONFIRMED",
           transaction_ref="new-in-tx",gross_filled_shares="5",net_position_delta_shares="5"),
      dict(event_id="late-terminal",kind="entry_order_terminal"),
      dict(event_id="late-order",kind="sell_order_accepted",order_id="late-sell",reserved_shares="5"),
      dict(event_id="late-out",kind="sell_fill",order_id="late-sell",trade_id="new-out",
           trade_status="CONFIRMED",transaction_ref="new-out-tx",filled_shares="5",reported_proceeds_usd="4")]
    for e in events:s=apply_event(s,e)
    return s

class PrefixTests(unittest.TestCase):
    def test_original_checkpoint_still_valid(self):
        self.assertTrue(complete()["cash_reusable"])
    def test_late_fill_invalidates_old_amount(self):
        s=late(complete())
        self.assertFalse(s["cash_reusable"])
        self.assertFalse(s["completed_roundtrip"])
        self.assertFalse(s["cash_amount_audit"]["amount_verified"])
    def test_audit_history_retained(self):
        self.assertEqual(len(late(complete())["cash_amount_audit_history"]),1)
    def test_new_complete_checkpoint_restores_validity(self):
        s=late(complete());op,flows,c=fixtures()
        flows += [
          dict(flows[0],event_id="late-in",economic_id="new-in",transaction_ref="new-in-tx",observed_ms=600,cash_delta_amount="-3"),
          dict(flows[1],event_id="late-out",economic_id="new-out",transaction_ref="new-out-tx",observed_ms=700,cash_delta_amount="4")]
        c.update(event_id="new-cash",observed_ms=800,balance_checkpoint_id="close-2",
                 balance_amount="53.30564",available_cash_amount="53.30564",
                 covered_event_ids=["entry","exit","late-in","late-out"],
                 opening_cash_checkpoint=op,cash_flow_evidence=flows)
        self.assertTrue(apply_event(s,c)["cash_reusable"])
    def test_reconcile_unknown_not_completed(self):
        s=apply_event(complete(),dict(event_id="unknown",kind="reconciliation_unknown"))
        self.assertFalse(s["cash_reusable"]);self.assertFalse(s["completed_roundtrip"])
        self.assertEqual(s["state"],"unknown_reconciliation")
    def test_conflicting_economic_receipt_rejected(self):
        s=exited(apply_event)
        e=dict(event_id="different-envelope",kind="entry_fill",trade_id="trade-in",trade_status="CONFIRMED",
               transaction_ref="fixture-tx-in",gross_filled_shares="6",net_position_delta_shares="6")
        self.assertRaisesRegex(ValueError,"CONFLICTING_ECONOMIC_RECEIPT",apply_event,s,e)
    def test_same_economic_receipt_other_event_id_is_idempotent(self):
        s=exited(apply_event)
        e=dict(event_id="different-envelope",kind="entry_fill",trade_id="trade-in",trade_status="CONFIRMED",
               transaction_ref="fixture-tx-in",gross_filled_shares="5",net_position_delta_shares="5")
        new=apply_event(s,e)
        self.assertEqual(new["entry_gross_filled_shares"],s["entry_gross_filled_shares"])
        self.assertEqual(new["cash_affecting_event_ids"],s["cash_affecting_event_ids"])
    def test_old_time_checkpoint_rejected(self):
        s=complete();op,flows,c=fixtures()
        c.update(event_id="regressing",observed_ms=499,opening_cash_checkpoint=op,cash_flow_evidence=flows)
        self.assertRaisesRegex(ValueError,"TIME_REGRESSION",apply_event,s,c)
    def test_new_event_does_not_mutate_input(self):
        s=complete();old=copy.deepcopy(s);late(s);self.assertEqual(s,old)
    def test_no_orders(self):
        self.assertFalse(late(complete())["orders_submitted_by_contract"])
if __name__=="__main__":unittest.main()
