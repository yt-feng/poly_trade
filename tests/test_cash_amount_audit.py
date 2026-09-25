from pathlib import Path
import copy,sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'reference'))
from cash_amount_audit import audit_cash_amount
from canary_receipt_amount_state import apply_event
try:
    from canary_receipt_state import new_episode,apply_event as legacy_apply
except ImportError:
    from canary_receipt_state_legacy import new_episode,apply_event as legacy_apply

ASSET='fixture:eip155:137/erc20:TEST_COLLATERAL'

def fixtures():
    op=dict(cash_asset=ASSET,account_id='fixture-account',balance_checkpoint_id='open-1',observed_ms=100,balance_amount='50')
    flows=[dict(event_id='entry',economic_id='trade-in',kind='entry_fill',account_id='fixture-account',cash_asset=ASSET,status='CONFIRMED',transaction_ref='fixture-tx-in',observed_ms=200,cash_delta_amount='-2.5875'),
           dict(event_id='exit',economic_id='trade-out',kind='sell_fill',account_id='fixture-account',cash_asset=ASSET,status='CONFIRMED',transaction_ref='fixture-tx-out',observed_ms=400,cash_delta_amount='4.89314')]
    cl=dict(event_id='cash',kind='cash_reconciled',cash_asset=ASSET,account_id='fixture-account',balance_checkpoint_id='close-1',observed_ms=500,balance_amount='52.30564',reserved_cash_amount='0',available_cash_amount='52.30564',complete_cash_flow_slice=True,covered_event_ids=['entry','exit'],position_checkpoint_id='pos-1',reconciled_position_shares='0')
    return op,flows,cl

def exited(apply=apply_event):
    s=new_episode(local_id='test',token_id='outcome-token',min_sell_size='5',expected_cash_asset=ASSET)
    events=[dict(event_id='entry',kind='entry_fill',trade_id='trade-in',trade_status='CONFIRMED',transaction_ref='fixture-tx-in',gross_filled_shares='5',net_position_delta_shares='5'),dict(event_id='terminal',kind='entry_order_terminal'),dict(event_id='sell',kind='sell_order_accepted',order_id='sell-1',reserved_shares='5'),dict(event_id='exit',kind='sell_fill',order_id='sell-1',trade_id='trade-out',trade_status='CONFIRMED',transaction_ref='fixture-tx-out',filled_shares='5',reported_proceeds_usd='4.89314')]
    for e in events:s=apply(s,e)
    return s

class AmountTests(unittest.TestCase):
    def run_audit(self,op=None,f=None,c=None):
        a,b,d=fixtures();return audit_cash_amount(a if op is None else op,b if f is None else f,d if c is None else c,['entry','exit'],ASSET)
    def test_consistent(self):self.assertTrue(self.run_audit()['amount_verified'])
    def test_not_external_verification(self):self.assertFalse(self.run_audit()['external_truth_verified'])
    def test_arbitrary_million_rejected(self):
        a,b,c=fixtures();c.update(balance_amount='1000000',available_cash_amount='1000000');self.assertEqual(self.run_audit(c=c)['reason'],'CASH_AMOUNT_MISMATCH')
    def test_wrong_asset(self):
        a,b,c=fixtures();c['cash_asset']='USDC-symbol-only';self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_wrong_account(self):
        a,b,c=fixtures();c['account_id']='other';self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_missing_open(self):self.assertFalse(self.run_audit(op={})['amount_verified'])
    def test_missing_flow(self):
        a,b,c=fixtures();self.assertFalse(self.run_audit(f=b[:1])['amount_verified'])
    def test_duplicate_economic(self):
        a,b,c=fixtures();b[1]['economic_id']=b[0]['economic_id'];self.assertFalse(self.run_audit(f=b)['amount_verified'])
    def test_unconfirmed(self):
        a,b,c=fixtures();b[0]['status']='MATCHED';self.assertFalse(self.run_audit(f=b)['amount_verified'])
    def test_wrong_buy_sign(self):
        a,b,c=fixtures();b[0]['cash_delta_amount']='2.5875';self.assertFalse(self.run_audit(f=b)['amount_verified'])
    def test_claim_not_cash(self):
        a,b,c=fixtures();b[1]['status']='REDEEMABLE';self.assertFalse(self.run_audit(f=b)['amount_verified'])
    def test_future_flow(self):
        a,b,c=fixtures();b[1]['observed_ms']=501;self.assertFalse(self.run_audit(f=b)['amount_verified'])
    def test_stale_checkpoint(self):
        a,b,c=fixtures();c['observed_ms']=300;self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_missing_coverage(self):
        a,b,c=fixtures();c['covered_event_ids']=['entry'];self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_extra_deposit_must_be_accounted(self):
        a,b,c=fixtures();c['balance_amount']='53.30564';c['available_cash_amount']='53.30564';self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_declared_deposit(self):
        a,b,c=fixtures();b.append(dict(b[0],event_id='deposit',economic_id='dep-1',kind='deposit',cash_delta_amount='1'));c.update(balance_amount='53.30564',available_cash_amount='53.30564',covered_event_ids=['entry','exit','deposit']);self.assertTrue(self.run_audit(f=b,c=c)['amount_verified'])
    def test_reserved_cash_not_free(self):
        a,b,c=fixtures();c.update(reserved_cash_amount='10',available_cash_amount='42.30564');self.assertTrue(self.run_audit(c=c)['amount_verified'])
    def test_reservation_mismatch(self):
        a,b,c=fixtures();c.update(reserved_cash_amount='10');self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_incomplete_slice(self):
        a,b,c=fixtures();c['complete_cash_flow_slice']=False;self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_nonflat(self):
        a,b,c=fixtures();c['reconciled_position_shares']='.01';self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_nan(self):
        a,b,c=fixtures();c['balance_amount']='NaN';self.assertFalse(self.run_audit(c=c)['amount_verified'])
    def test_adapter_good(self):
        a,b,c=fixtures();c.update(opening_cash_checkpoint=a,cash_flow_evidence=b);s=apply_event(exited(),c);self.assertTrue(s['cash_reusable'])
    def test_legacy_arbitrary_balance_reproduced(self):
        a,b,c=fixtures();c.update(available_cash_amount='1000000');self.assertTrue(legacy_apply(exited(legacy_apply),c)['cash_reusable'])
    def test_adapter_blocks_legacy_arbitrary_balance(self):
        a,b,c=fixtures();c.update(balance_amount='1000000',available_cash_amount='1000000',opening_cash_checkpoint=a,cash_flow_evidence=b);s=apply_event(exited(),c);self.assertFalse(s['cash_reusable']);self.assertTrue(s['halt_new_actions'])
    def test_adapter_missing_amounts(self):
        a,b,c=fixtures();self.assertFalse(apply_event(exited(),c)['cash_reusable'])
    def test_idempotent(self):
        a,b,c=fixtures();c.update(opening_cash_checkpoint=a,cash_flow_evidence=b);s=apply_event(exited(),c);self.assertEqual(s,apply_event(s,c))
    def test_conflicting_duplicate(self):
        a,b,c=fixtures();c.update(opening_cash_checkpoint=a,cash_flow_evidence=b);s=apply_event(exited(),c);c['available_cash_amount']='999';self.assertRaises(ValueError,apply_event,s,c)
    def test_no_mutation_on_failure(self):
        s=exited();old=copy.deepcopy(s);self.assertRaises(ValueError,apply_event,s,dict(kind='anything'));self.assertEqual(s,old)
    def test_no_execution(self):
        a,b,c=fixtures();c.update(opening_cash_checkpoint=a,cash_flow_evidence=b);self.assertFalse(apply_event(exited(),c)['orders_submitted_by_contract'])

if __name__=='__main__':unittest.main()
