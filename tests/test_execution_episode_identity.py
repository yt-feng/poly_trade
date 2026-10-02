"""Synthetic identity/snapshot accounting through the existing summarize entry.

No network, real account, signal, execution client or private research result.
"""
from pathlib import Path
import copy
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from execution_reason_codes import summarize, classify


def record(**updates):
    r=dict(execution_identity=dict(account_scope='chain:account-fixture',collateral_asset='asset-fixture',
        condition_id='condition-fixture',episode_id='episode-fixture',token_id='token-up'),
        observation_ms=1000,submission_reference='order-fixture',strategy_id='model-a',
        signal_qualified=True,planned_shares='5',min_order_size='5',requested_shares='5',
        confirmed_filled_shares='5',remaining_position_shares='0',entry_order_terminal=True,
        order_terminal=True,order_status='CONFIRMED',cash_reconciled=True,position_flat=True)
    r.update(updates)
    return r


def identity(r,**updates):
    out=copy.deepcopy(r);out['execution_identity'].update(updates);return out


class EpisodeIdentityTests(unittest.TestCase):
    def audit(self,rows,at=5000):return summarize(rows,as_of_ms=at)['episode_audit']
    def issue(self,rows,code,at=5000):
        q=self.audit(rows,at);self.assertEqual(q['issues'],[code]);self.assertIsNone(q['unique_episode_count'])
        self.assertIsNone(q['supplied_confirmed_entry_episodes']);self.assertIsNone(q['supplied_cash_closed_episodes'])
    def test_ten_duplicate_views_are_one_declared_roundtrip_not_ten(self):
        rows=[record(strategy_id='model-'+str(i)) for i in range(10)]
        s=summarize(rows,as_of_ms=5000);q=s['episode_audit']
        self.assertEqual(s['episodes'],10);self.assertTrue(s['legacy_counts_are_row_based'])
        self.assertEqual((q['unique_episode_count'],q['supplied_confirmed_entry_episodes'],q['supplied_cash_closed_episodes']),(1,1,1))
        self.assertEqual(q['redundant_or_historical_records'],9)
    def test_partial_fill_snapshots_are_cumulative_not_additive(self):
        a=record(confirmed_filled_shares='2',cash_reconciled=False,position_flat=False,observation_ms=900)
        q=self.audit([a,record()]);self.assertEqual(q['supplied_confirmed_entry_episodes'],1)
    def test_latest_snapshot_not_list_order(self):
        old=record(observation_ms=900,cash_reconciled=False,position_flat=False)
        self.assertEqual(self.audit([old,record()]),self.audit([record(),old]))
    def test_late_unknown_invalidates_old_cash_closure(self):
        q=self.audit([record(),record(observation_ms=1100,reconciliation_unknown=True)])
        self.assertEqual(q['supplied_confirmed_entry_episodes'],1);self.assertEqual(q['supplied_cash_closed_episodes'],0)
        self.assertEqual(q['nonfinal_episode_count'],1);self.assertEqual(q['by_latest_reason_code'],{'RECONCILIATION_UNKNOWN':1})
    def test_equal_time_conflicting_economic_facts_not_arbitrarily_selected(self):
        self.issue([record(),record(cash_reconciled=False)],'LATEST_SNAPSHOT_CONFLICT')
    def test_model_scores_are_annotations_not_trade_identity(self):
        q=self.audit([record(model_id='a',model_score=.7),record(model_id='b',model_score=.8)])
        self.assertEqual(q['unique_episode_count'],1)
    def test_undocumented_economic_field_is_not_dropped(self):
        self.issue([record(net_proceeds='1'),record(net_proceeds='2')],'LATEST_SNAPSHOT_CONFLICT')
    def test_future_state_not_used_to_certify_past(self):
        self.issue([record(),record(observation_ms=5001)],'FUTURE_OBSERVATION_NOT_USABLE')
    def test_no_asof_no_strict_count(self):
        self.issue([record()],'EXPLICIT_AS_OF_REQUIRED',at=None)
    def test_boolean_asof_not_integer_time(self):
        self.issue([record()],'EXPLICIT_AS_OF_REQUIRED',at=True)
    def test_missing_time_is_unknown_not_current_time(self):
        r=record();del r['observation_ms'];self.issue([r],'EXPLICIT_OBSERVATION_TIME_REQUIRED')
    def test_boolean_observation_time_rejected(self):
        self.issue([record(observation_ms=True)],'EXPLICIT_OBSERVATION_TIME_REQUIRED')
    def test_mixed_identified_and_unidentified_rows_not_counted_as_complete(self):
        r=record();del r['execution_identity'];self.issue([record(),r],'COMPLETE_EXECUTION_IDENTITY_REQUIRED')
    def test_blank_or_missing_identity_not_auto_generated(self):
        for field in ('account_scope','collateral_asset','condition_id','episode_id','token_id'):
            with self.subTest(field=field):self.issue([identity(record(),**{field:''})],'COMPLETE_EXECUTION_IDENTITY_REQUIRED')
    def test_accounts_not_accidentally_deduplicated(self):
        q=self.audit([record(),identity(record(),account_scope='chain:second-account')]);self.assertEqual(q['unique_episode_count'],2)
    def test_assets_not_accidentally_deduplicated(self):
        q=self.audit([record(),identity(record(),collateral_asset='second-asset')]);self.assertEqual(q['unique_episode_count'],2)
    def test_markets_not_accidentally_deduplicated(self):
        r=identity(record(submission_reference='order-second'),condition_id='second-condition')
        self.assertEqual(self.audit([record(),r])['unique_episode_count'],2)
    def test_opposite_tokens_in_different_episodes_do_not_net_to_zero_risk(self):
        a=record(cash_reconciled=False,position_flat=False)
        b=identity(record(submission_reference='order-second',cash_reconciled=False,position_flat=False),episode_id='second',token_id='token-down')
        q=self.audit([a,b]);self.assertEqual(q['unique_episode_count'],2);self.assertEqual(q['nonfinal_episode_count'],2)
        self.assertEqual(q['supplied_cash_closed_episodes'],0)
    def test_opposite_tokens_under_one_episode_are_identity_conflict(self):
        self.issue([record(),identity(record(),token_id='token-down')],'TOKEN_IDENTITY_CONFLICT_WITHIN_EPISODE')
    def test_same_order_alias_under_two_strategy_episode_ids_rejected(self):
        self.issue([record(),identity(record(),episode_id='model-b-episode')],'SUBMISSION_ALIASED_ACROSS_EPISODES')
    def test_cumulative_confirmed_amount_must_not_reset_after_exit(self):
        b=record(observation_ms=1100,confirmed_filled_shares='0',cash_reconciled=False,position_flat=False)
        self.issue([record(),b],'CUMULATIVE_CONFIRMED_QUANTITY_REGRESSED')
    def test_missing_latest_quantity_not_defaulted_to_zero(self):
        r=record(cash_reconciled=False,position_flat=False);del r['confirmed_filled_shares']
        self.issue([r],'LATEST_CONFIRMED_QUANTITY_UNKNOWN')
    def test_positive_fill_without_order_identity_is_unverified(self):
        r=record();del r['submission_reference'];self.issue([r],'CONFIRMED_ENTRY_ORDER_IDENTITY_MISSING')
    def test_terminal_explicit_zero_counts_no_entry(self):
        r=record(confirmed_filled_shares='0',cash_reconciled=False,position_flat=False,
            order_status='',fill_reconciliation_complete=True)
        q=self.audit([r]);self.assertEqual(q['supplied_confirmed_entry_episodes'],0)
        self.assertEqual(q['supplied_cash_closed_episodes'],0)
    def test_matched_without_confirmation_is_not_an_entry(self):
        r=record(confirmed_filled_shares='0',cash_reconciled=False,position_flat=False,order_status='MATCHED',
            order_terminal=False,entry_order_terminal=False,matched_shares='2')
        q=self.audit([r]);self.assertEqual(q['supplied_confirmed_entry_episodes'],0);self.assertEqual(q['nonfinal_episode_count'],1)
    def test_duplicate_cash_and_pnl_are_not_summed(self):
        q=self.audit([record(net_proceeds='10'),record(net_proceeds='10',strategy_id='b')])
        self.assertFalse(q['cash_or_pnl_aggregated']);self.assertNotIn('net_proceeds',q)
    def test_no_authentication_or_live_approval(self):
        q=self.audit([record()])
        for field in ('private_receipts_authenticated','canary_eligibility_evaluated','live_action_taken'):
            self.assertIs(q[field],False)
    def test_input_not_mutated(self):
        rows=[record(),record(strategy_id='b')];saved=copy.deepcopy(rows);self.audit(rows);self.assertEqual(rows,saved)
    def test_empty_valid_asof_has_zero_not_a_fill(self):
        q=self.audit([]);self.assertEqual(q['unique_episode_count'],0);self.assertEqual(q['supplied_confirmed_entry_episodes'],0)
    def test_legacy_unidentified_summary_stays_explicitly_row_based(self):
        r={'signal_qualified':False};s=summarize([r,r])
        self.assertEqual(s['episodes'],2);self.assertEqual(s['record_count'],2)
        self.assertIsNone(s['episode_audit']['unique_episode_count'])
    def test_classification_of_individual_records_unchanged(self):
        rows=[record(),record(reconciliation_unknown=True)]
        before=[classify(r) for r in rows];summarize(rows);self.assertEqual([classify(r) for r in rows],before)
    def test_asof_boundary_is_inclusive(self):
        self.assertEqual(self.audit([record()],at=1000)['unique_episode_count'],1)

if __name__=='__main__':unittest.main()
