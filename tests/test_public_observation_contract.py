from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from public_observation_contract import assess

def event(**kw):
    d=dict(wallet='fixture-A',event_ms=2000,received_ms=2100,condition_id='fixture-market',token_id='fixture-up',
           transaction_hash='fixture-tx',log_index=1,chain_id=137,role='taker',role_source='settlement',kind='fill',side='BUY',shares='10',price='.2')
    d.update(kw);return d

def check(e=None,**kw):
    args=dict(decision_ms=2200,cohort_locked_ms=1000,cohort_cutoff_ms=900,cohort_wallets=['fixture-A'],
              condition_id='fixture-market',outcome_tokens=['fixture-up','fixture-down'])
    args.update(kw);return assess(event() if e is None else e,**args)

class PublicObservationTests(unittest.TestCase):
    def test_valid_is_not_authenticated(self):
        r=check();self.assertTrue(r['usable_asof_observation']);self.assertFalse(r['source_authenticated'])
    def test_no_recommendation(self):self.assertFalse(check()['is_trade_recommendation'])
    def test_not_quote_history(self):self.assertFalse(check()['wallet_quote_lifecycle_observed'])
    def test_current_winners_cannot_select_old_trades(self):self.assertFalse(check(cohort_locked_ms=2300)['usable_asof_observation'])
    def test_future_cohort_information(self):self.assertFalse(check(cohort_cutoff_ms=1200)['usable_asof_observation'])
    def test_late_publication(self):self.assertFalse(check(event(received_ms=2300))['usable_asof_observation'])
    def test_future_event(self):self.assertFalse(check(event(event_ms=2400))['usable_asof_observation'])
    def test_wrong_wallet(self):self.assertFalse(check(event(wallet='fixture-B'))['usable_asof_observation'])
    def test_wrong_condition(self):self.assertFalse(check(event(condition_id='another'))['usable_asof_observation'])
    def test_wrong_token(self):self.assertFalse(check(event(token_id='another'))['usable_asof_observation'])
    def test_missing_log_index(self):self.assertFalse(check(event(log_index=None))['usable_asof_observation'])
    def test_inferred_role_not_truth(self):self.assertFalse(check(event(role_source='tick-rule'))['usable_asof_observation'])
    def test_merge_not_direction(self):self.assertFalse(check(event(kind='merge'))['usable_asof_observation'])
    def test_nan(self):self.assertFalse(check(event(price='NaN'))['usable_asof_observation'])
    def test_boolean_timestamp(self):self.assertFalse(check(event(received_ms=True))['usable_asof_observation'])
    def test_duplicate_economic_identity(self):self.assertEqual(check()['economic_event_id'],check(event(received_ms=2150))['economic_event_id'])
    def test_notional_not_share_count(self):self.assertEqual(check()['observed_notional'],'2.0')
    def test_missing_side(self):self.assertFalse(check(event(side=None))['usable_asof_observation'])

if __name__=='__main__':unittest.main()
