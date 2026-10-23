import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from analysis import offline_exit_continuation_audit as audit

spec=importlib.util.spec_from_file_location('continuation_synthetic_inputs',Path(__file__).with_name('test_offline_exit_retry_audit.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


class ContinuationAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        probe=patch.object(audit.prior,'probe_runtime_environment',return_value=fixtures.runtime_fixture());self.probe=probe.start();self.addCleanup(probe.stop)
        self.base=fixtures.protocol();self.rows=[];self.raw=[]
        self.source=self.root/'raw';(self.source/'run').mkdir(parents=True);self.quote_path=self.source/'run/part-btc-updown-5m_quotes.csv'
        for i in range(12):self.add_market(i*1300)
        self.write_inputs()
        self.parent={'evaluation_mode':'offline_exit_retry','source_commit':'a'*40,'expected_code_commit':'b'*40,'source_repository':'synthetic/archive',
            'base_protocol':self.base,'base_protocol_sha256':audit.prior.digest(audit.prior.canonical(self.base)),
            'candidate_binding':self.binding,'frozen_control_candidate_fields':list(audit.prior.CANDIDATE_FIELDS),'frozen_control_candidates':self.rows,
            'frozen_control_candidate_rows_sha256':audit.prior.digest(audit.prior.canonical(self.rows)),'expected_control_candidate_count':len(self.rows),
            'expected_original_pooled_fingerprint':'0'*64,'retry_policy':{'reaction_delay_seconds':4.,'order_latency_seconds':8.,'max_quote_delay_seconds':3.,
                'deadline_after_original_target_seconds':30.,'max_extra_attempts':2},'raw_budget':dict(audit.prior.HARD_RAW),
            'code_file_sha256':{k:'d'*64 for k in audit.prior.REQUIRED_CODE_FILES},'actual_model_fit_calls':0,'actual_model_predict_calls':0,
            'max_producer_replays':2,'live_orders_enabled':False,'environment':{'python_major_minor':'3.11','packages':fixtures.runtime_fixture()['packages']}}
        self.reference=self.old_stopping_reference();self.config=self.outer_config()
    def add_market(self,shift,exit_bid=60.,exit_size=1e9):
        start=fixtures.START+pd.Timedelta(seconds=shift);slug='btc-updown-5m-'+str(int(start.timestamp()))
        row={**fixtures.candidate(),'market_id':'run::'+slug,'slug':slug,'decision_ts':(start+pd.Timedelta(seconds=120)).isoformat(),
            'market_close_ts':(start+pd.Timedelta(seconds=300)).isoformat(),'decision_sell_size':'1000000000'}
        self.rows.append(row)
        entry=fixtures.quote(slug,shift+132);exit_=fixtures.quote(slug,shift+170,bid=exit_bid,size=exit_size)
        for side in ('up','down'):entry[f'buy_{side}_size']=1e9
        self.raw.extend([entry,exit_])
    def write_inputs(self):
        self.candidate_path=self.root/'candidates.csv';self.binding=fixtures.write_csv(self.candidate_path,self.rows,list(audit.prior.CANDIDATE_FIELDS))
        fixtures.write_csv(self.quote_path,self.raw,['slug','ts_iso',*audit.prior.book_base.BOOK_NUMERIC])
    def source_and_candidates(self):
        candidates=audit.prior.load_frozen_candidates(self.candidate_path,self.binding,self.rows)
        source,manifest=audit.prior.load_candidate_quote_books(self.source,candidates,self.base,audit.prior.HARD_RAW,deadline_after_original_target_seconds=30.)
        return source,manifest,candidates
    def old_stopping_reference(self):
        from scheduled_exit_retry import RetryPolicy,ScheduledExitResolver
        source,_,candidates=self.source_and_candidates();source.phase='C_scheduled_retry'
        result=audit.prior.execution.replay_cached_quotes(candidates,['run'],source,
            exit_resolver=audit.prior.ExitEvidenceCollector(ScheduledExitResolver(RetryPolicy(**self.parent['retry_policy']))),**audit.prior.replay_kwargs(self.base))
        return audit.prior.pooled_wrapper(result,self.base)
    def outer_config(self):
        raw=audit.prior.canonical(audit.prior.json_safe(self.reference))+b'\n'
        return {'evaluation_mode':'offline_exit_continuation','parent_strategy_config':copy.deepcopy(self.parent),
            'parent_strategy_config_sha256':audit.prior.digest(audit.prior.canonical(self.parent)),
            'source_commit':self.parent['source_commit'],'source_repository':self.parent['source_repository'],'candidate_binding':copy.deepcopy(self.binding),
            'expected_code_commit':'c'*40,'code_file_sha256':{k:'e'*64 for k in audit.REQUIRED_CODE_FILES},
            'prefix_binding':{'frozen_pooled_result':copy.deepcopy(self.reference),
                'expected_pooled_fingerprint':audit.prior.fingerprints.result_fingerprint(self.reference),
                'parent_retry_ledger_bytes':len(raw),'parent_retry_ledger_sha256':audit.prior.digest(raw)},
            'max_producer_replays':1,'actual_model_fit_calls':0,'actual_model_predict_calls':0,'live_orders_enabled':False}
    def run_continuation(self,output=None):
        with patch.object(audit.prior,'verified_code_source',return_value={'source_commit':'a'*40,'code_commit':'c'*40}) as verify:
            result=audit.evaluate(self.source,self.candidate_path,self.config,output or self.root/'out')
        self.assertIs(verify.call_args.args[1],self.config)
        return result
    def test_single_replay_continues_original_bankroll_and_keeps_exact_prefix(self):
        original=audit.prior.execution.replay_cached_quotes
        with patch.object(audit.prior.execution,'replay_cached_quotes',wraps=original) as replay:
            result=self.run_continuation()
        self.assertEqual(replay.call_count,1);self.assertTrue(replay.call_args.kwargs['continue_after_required_fills'])
        self.assertEqual(result['prefix_ledger'],self.reference);self.assertTrue(result['prefix_control_reproduced'])
        full=result['full_ledger'];self.assertEqual(full['fills'],12);self.assertEqual(full['resolved_fills'],12)
        self.assertEqual(full['chain'][:10],self.reference['chain']);self.assertEqual(full['chain'][10]['cash_before_entry'],self.reference['ending_cash'])
        self.assertNotEqual(full['first_ten_ending_cash'],full['ending_cash']);self.assertEqual(full['first_ten_ending_cash'],self.reference['ending_cash'])
        self.assertEqual(result['replayed_cells'],1);self.assertEqual(result['producer_replay_completions'],1);self.assertEqual(result['actual_model_fit_calls'],0)
        self.assertEqual(result['processed_candidate_count'],12);self.assertEqual(result['required_prefix_processed_candidate_count'],10)
        self.assertTrue(result['candidate_stream_exhausted']);self.assertTrue(result['full_economic_pass'])
        self.assertEqual(set(p.name for p in (self.root/'out').iterdir()),audit.OUTPUTS)
        self.assertEqual(result['cash_time_readouts']['tail']['entered_positions_after_prefix'],2)
        self.assertTrue(result['cash_time_readouts']['full']['fully_resolved'])
    def test_reference_guard_raises_before_any_postprefix_selector(self):
        frozen=self.config['prefix_binding']['frozen_pooled_result'];frozen['chain'][0]['entry_fee']+=1
        proof=self.config['prefix_binding'];raw=audit.prior.canonical(frozen)+b'\n'
        proof.update(expected_pooled_fingerprint=audit.prior.fingerprints.result_fingerprint(frozen),parent_retry_ledger_bytes=len(raw),parent_retry_ledger_sha256=audit.prior.digest(raw))
        source,manifest,_=self.source_and_candidates()
        with patch.object(audit.prior,'verified_code_source',return_value={}),patch.object(audit.prior,'load_candidate_quote_books',return_value=(source,manifest)):
            with self.assertRaisesRegex(ValueError,'evaluation_failed'):audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
        self.assertEqual(len(source.requests),20)
        self.assertNotIn(self.rows[10]['market_id'],[r['market_id'] for r in source.requests])
        result=json.loads((self.root/'out/summary.json').read_text())
        self.assertFalse(result['prefix_control_reproduced']);self.assertEqual(result['producer_replay_completions'],0)
        self.assertTrue((self.root/'out/prefix-ledger.json').exists());self.assertFalse((self.root/'out/full-ledger.json').exists())
    def test_pending_boundary_not_advanced_or_credited_twice(self):
        release=audit.prior._time(self.reference['chain'][-1]['cash_release_ts'])
        self.add_market(int((release-fixtures.START).total_seconds())-132)
        self.rows.sort(key=lambda r:r['decision_ts']);self.write_inputs()
        self.parent.update(candidate_binding=self.binding,frozen_control_candidates=self.rows,expected_control_candidate_count=len(self.rows),
            frozen_control_candidate_rows_sha256=audit.prior.digest(audit.prior.canonical(self.rows)))
        self.config=self.outer_config();result=self.run_continuation();full=result['full_ledger']
        blocked=next(r for r in full['attempts'] if r['status']=='prior_cash_not_released')
        self.assertEqual(audit.prior._time(blocked['entry_target_ts']),release)
        self.assertEqual(full['chain'][10]['cash_before_entry'],self.reference['ending_cash'])
        self.assertEqual(result['prefix_ledger'],self.reference);self.assertEqual(result['processed_candidate_count'],13)
    def test_postprefix_lock_does_not_inherit_green_full_endpoint(self):
        slug=self.rows[10]['slug']
        for row in self.raw:
            if row['slug']==slug and audit.prior._time(row['ts_iso'])==audit.prior._time(self.rows[10]['decision_ts'])+pd.Timedelta(seconds=50):
                row['sell_up_size']=row['sell_down_size']=0.
        self.write_inputs();result=self.run_continuation();full=result['full_ledger']
        self.assertTrue(result['prefix_economic_pass']);self.assertFalse(result['full_economic_pass']);self.assertIsNone(result['full_net_growth'])
        self.assertGreater(full['locked_shares'],0);self.assertEqual(full['fills'],11);self.assertEqual(result['unprocessed_candidate_count'],1)
        self.assertTrue(result['comparison_valid']);self.assertIsNone(result['cash_time_readouts']['full']['elapsed_first_entry_to_final_assumed_release_seconds'])
        self.assertEqual(result['cash_time_readouts']['tail']['unresolved_positions_after_prefix'],1)
        self.assertNotIn('total_wealth',result['cash_time_readouts']['tail'])
    def test_postprefix_loss_can_cause_original_minimum_cash_stop(self):
        slug=self.rows[10]['slug']
        for row in self.raw:
            if row['slug']==slug and audit.prior._time(row['ts_iso'])==audit.prior._time(self.rows[10]['decision_ts'])+pd.Timedelta(seconds=50):
                row['sell_up_cents']=row['sell_down_cents']=5.
        self.write_inputs();result=self.run_continuation();full=result['full_ledger']
        self.assertTrue(result['prefix_economic_pass']);self.assertEqual(full['continuation_failure_reason'],'cash_cannot_fit_minimum_order')
        self.assertFalse(result['full_economic_pass']);self.assertLess(full['ending_cash'],self.reference['ending_cash'])
        self.assertEqual(full['attempts'][-1]['status'],'cash_cannot_fit_minimum_order');self.assertEqual(full['first_ten_ending_cash'],self.reference['ending_cash'])
    def test_incomplete_prefix_is_failure_not_reset_or_second_replay(self):
        slug=self.rows[1]['slug'];self.raw=[r for r in self.raw if not (r['slug']==slug and audit.prior._time(r['ts_iso'])==audit.prior._time(self.rows[1]['decision_ts'])+pd.Timedelta(seconds=50))]
        self.write_inputs();original=audit.prior.execution.replay_cached_quotes
        with patch.object(audit.prior,'verified_code_source',return_value={}),patch.object(audit.prior.execution,'replay_cached_quotes',wraps=original) as replay:
            with self.assertRaisesRegex(ValueError,'evaluation_failed'):audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
        self.assertEqual(replay.call_count,1);result=json.loads((self.root/'out/summary.json').read_text())
        self.assertFalse(result['prefix_control_reproduced']);self.assertEqual(result['producer_replay_completions'],1)
        self.assertTrue((self.root/'out/full-ledger.json').exists());self.assertFalse((self.root/'out/prefix-ledger.json').exists())
    def test_parent_and_prefix_byte_binding_fail_before_candidate_raw_or_replay(self):
        for mutation in ('parent_hash','source','candidate','prefix_byte','prefix_fp','budget','old_code_guard'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                config=copy.deepcopy(self.config)
                if mutation=='parent_hash':config['parent_strategy_config_sha256']='0'*64
                elif mutation=='source':config['source_commit']='e'*40
                elif mutation=='candidate':config['candidate_binding']['sha256']='0'*64
                elif mutation=='prefix_byte':config['prefix_binding']['parent_retry_ledger_bytes']+=1
                elif mutation=='prefix_fp':config['prefix_binding']['expected_pooled_fingerprint']='0'*64
                elif mutation=='budget':config['max_producer_replays']=2
                else:config['code_file_sha256'].pop('analysis/offline_exit_continuation_audit.py')
                with patch.object(audit.prior,'verified_code_source',side_effect=AssertionError('no source')) as verified, \
                        patch.object(audit.prior,'load_frozen_candidates',side_effect=AssertionError('no candidate read')) as loaded, \
                        patch.object(audit.prior.execution,'replay_cached_quotes',side_effect=AssertionError('no replay')) as replay:
                    with self.assertRaisesRegex(ValueError,'evaluation_failed'):audit.evaluate(self.source,self.candidate_path,config,Path(tmp)/'out')
                self.assertEqual([verified.call_count,loaded.call_count,replay.call_count],[0,0,0])
    def test_runtime_failure_still_precedes_all_data(self):
        wrong=fixtures.runtime_fixture();wrong['loaded_module_versions']['pandas']='9.9.9';self.probe.return_value=wrong
        with patch.object(audit.prior,'verified_code_source',side_effect=AssertionError('no source')) as verified:
            with self.assertRaisesRegex(ValueError,'evaluation_failed'):audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
        self.assertEqual(verified.call_count,0);result=json.loads((self.root/'out/summary.json').read_text())
        self.assertEqual(result['runtime_environment'],wrong);self.assertEqual(result['replayed_cells'],0)
    def test_new_collector_accepts_more_than_old_thirty_without_changing_old_default(self):
        from scheduled_exit_retry import RetryPolicy,ScheduledExitResolver
        # Twelve originals are below old30; construct a standalone annotation
        # stub with three saved trace rows per callback to test aggregate bound.
        class Resolution:
            trace=({'status':'synthetic'},)*3
        class Resolver:
            def __call__(self,*args):return Resolution()
        collector=audit.ContinuationEvidenceCollector(Resolver(),36)
        class Request:market_id='synthetic'
        for _ in range(12):collector(Request(),None,None,None)
        self.assertEqual(len(collector.attempt_evidence),36)
        with self.assertRaisesRegex(ValueError,'row_budget'):collector(Request(),None,None,None)
        self.assertEqual(len(collector.attempt_evidence),36)
    def test_full_readout_keeps_missing_clock_unknown_and_preserves_tail_idle_context(self):
        result=self.run_continuation();full=copy.deepcopy(result['full_ledger']);prefix=result['prefix_ledger']
        readout=audit.cash_time_readouts(full,prefix,self.base)
        self.assertEqual(readout['tail']['positions'][0],readout['full']['positions'][10])
        self.assertGreater(readout['tail']['positions'][0]['idle_after_previous_assumed_release_seconds'],0)
        full['chain'][-1].pop('cash_release_ts');readout=audit.cash_time_readouts(full,prefix,self.base)
        self.assertIsNone(readout['full']['elapsed_first_entry_to_final_assumed_release_seconds']);self.assertEqual(readout['full']['elapsed_status'],'incomplete_or_unknown')

if __name__=='__main__':unittest.main()
