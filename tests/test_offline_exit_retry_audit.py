import base64
import copy
import csv
import io
import json
import lzma
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from analysis import offline_exit_retry_audit as audit

START=pd.Timestamp('2026-01-01T00:00:00Z')


def protocol():
    return {'protocol_id':'synthetic','evaluation_mode':'book_bid_residual','source_commit':'a'*40,'expected_code_commit':'b'*40,
        'features':audit.book_base.FEATURES.copy(),'source_runs':['past','run'],'run_order':['past','run'],
        'expected_files_per_run':1,'minimum_prefix_quotes':3,'minimum_training_runs':1,'minimum_training_labels':1,
        'evaluation_block_runs':12,'max_source_rows':100,'max_feature_rows':100,'max_scored_runs':10,'max_replay_cells':8,
        'opening_tolerance_seconds':3.,'maximum_snapshot_age_seconds':3.,'decision_cutoff_seconds':120.,'entry_delay_seconds':4.,
        'hold_seconds':30.,'latency_seconds':8.,'max_quote_delay_seconds':3.,'edge_floor':.02,'fee_rate':.07,'slippage':.01,
        'depth_haircut':.1,'minimum_order_usd':5.,'minimum_order_shares':5.,'depth_thresholds':[3.,5.,7.],
        'cash_release_delay_after_close_seconds':900.,'ridge_alpha':1.,'ridge_solver':'svd','initial_cash':10.,'required_fills':10}

def candidate():
    slug='btc-updown-5m-'+str(int(START.timestamp()))
    return {'market_id':'run::'+slug,'run_name':'run','slug':slug,'side':'up','decision_ts':(START+pd.Timedelta(seconds=120)).isoformat(),
        'market_close_ts':(START+pd.Timedelta(seconds=300)).isoformat(),'decision_ask_price':'.5','decision_sell_size':'1000','predicted_exit_bid_price':'.8'}

def quote(slug,second,bid=70.,size=1000.):
    row={'slug':slug,'ts_iso':(START+pd.Timedelta(seconds=second)).isoformat()}
    for side in ('up','down'):row.update({f'buy_{side}_cents':50.,f'sell_{side}_cents':bid,f'buy_{side}_size':1000.,f'sell_{side}_size':size})
    return row

def write_csv(path,rows,fields):
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=fields,lineterminator='\n');writer.writeheader();writer.writerows(rows)
    raw=stream.getvalue().encode();path.write_bytes(raw)
    return {'bytes':len(raw),'sha256':audit.digest(raw),'expected_header':fields,'expected_rows':len(rows)}

class ExitRetryAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.parent=protocol();self.row=candidate();self.candidate_path=self.root/'candidate.csv'
        self.binding=write_csv(self.candidate_path,[{**self.row,'future_ignored':'NaN/unavailable'}],[*audit.CANDIDATE_FIELDS,'future_ignored'])
        self.source=self.root/'raw';(self.source/'run').mkdir(parents=True)
        self.quote_path=self.source/'run'/'part-btc-updown-5m_quotes.csv'
        self.raw=[quote(self.row['slug'],132),quote(self.row['slug'],170,size=0),quote(self.row['slug'],182,bid=40)]
        self.write_quotes()
        self.config={'evaluation_mode':'offline_exit_retry','source_commit':'a'*40,'expected_code_commit':'c'*40,'source_repository':'synthetic/archive',
            'base_protocol':self.parent,'base_protocol_sha256':audit.digest(audit.canonical(self.parent)),
            'candidate_binding':self.binding,'frozen_control_candidate_fields':list(audit.CANDIDATE_FIELDS),'frozen_control_candidates':[self.row],
            'frozen_control_candidate_rows_sha256':audit.digest(audit.canonical([self.row])),'expected_control_candidate_count':1,
            'expected_original_pooled_fingerprint':'0'*64,'retry_policy':{'reaction_delay_seconds':4.,'order_latency_seconds':8.,'max_quote_delay_seconds':3.,
                'deadline_after_original_target_seconds':30.,'max_extra_attempts':2},'raw_budget':dict(audit.HARD_RAW),
            'code_file_sha256':{k:'d'*64 for k in audit.REQUIRED_CODE_FILES},'actual_model_fit_calls':0,'actual_model_predict_calls':0,
            'max_producer_replays':2,'live_orders_enabled':False}
    def write_quotes(self):
        write_csv(self.quote_path,self.raw,['slug','ts_iso',*audit.book_base.BOOK_NUMERIC])
    def load_candidates(self):return audit.load_frozen_candidates(self.candidate_path,self.binding,[self.row])
    def load_source(self):return audit.load_candidate_quote_books(self.source,self.load_candidates(),self.parent,self.config['raw_budget'],deadline_after_original_target_seconds=30.)
    def baseline(self):
        source,_=self.load_source();source.phase='synthetic_reference'
        result=audit.execution.replay_cached_quotes(self.load_candidates(),['run'],source,**audit.replay_kwargs(self.parent))
        return audit.pooled_wrapper(result,self.parent)
    def evaluate(self):
        self.config['expected_original_pooled_fingerprint']=audit.fingerprints.result_fingerprint(self.baseline())
        with patch.object(audit,'verified_code_source',return_value={'source_commit':'a'*40,'code_commit':'c'*40}):
            return audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
    def test_exact_projection_excludes_future_columns_and_preserves_float(self):
        frame=self.load_candidates();self.assertEqual(tuple(frame.columns),audit.CANDIDATE_FIELDS)
        self.assertEqual(frame.iloc[0].decision_ask_price,.5);self.assertNotIn('future_ignored',frame)
    def test_candidate_bytes_header_order_manifest_and_domains(self):
        mutations=[lambda: self.binding.update(sha256='0'*64),lambda:self.binding.update(bytes=self.binding['bytes']+1),
            lambda:self.binding.update(expected_header=list(reversed(self.binding['expected_header']))),lambda:self.row.update(decision_ask_price='.6'),
            lambda:self.row.update(decision_ts='2026-01-01T00:02:00.000000001Z')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                binding,row=copy.deepcopy(self.binding),copy.deepcopy(self.row);mutate()
                with self.assertRaises(ValueError):self.load_candidates()
                self.binding,self.row=binding,row
    def test_duplicate_candidates_and_unqualified_time_rejected(self):
        self.binding=write_csv(self.candidate_path,[self.row,self.row],list(audit.CANDIDATE_FIELDS))
        with self.assertRaisesRegex(ValueError,'duplicate'):audit.load_frozen_candidates(self.candidate_path,self.binding,[self.row,self.row])
        self.row['decision_ts']='2026-01-01T00:02:00'
        self.binding=write_csv(self.candidate_path,[self.row],list(audit.CANDIDATE_FIELDS))
        with self.assertRaisesRegex(ValueError,'timezone'):self.load_candidates()
    def test_timezone_equivalent_manifest_and_nonfinite_number(self):
        saved=copy.deepcopy(self.row);saved['decision_ts']='2026-01-01 08:02:00+08:00'
        self.assertEqual(audit.load_frozen_candidates(self.candidate_path,self.binding,[saved]).iloc[0].decision_ts,START+pd.Timedelta(seconds=120))
        saved['decision_ask_price']='inf'
        with self.assertRaisesRegex(ValueError,'finite'):audit.load_frozen_candidates(self.candidate_path,self.binding,[saved])
    def test_candidate_transport_eof_hash_and_expansion(self):
        raw=self.candidate_path.read_bytes();packet=base64.b64encode(lzma.compress(raw)).decode()
        self.assertEqual(audit.decode_candidate_packet(packet,self.binding),raw)
        for packed in (lzma.compress(raw)+b'trailing',lzma.compress(raw)+lzma.compress(raw),lzma.compress(raw+b'x')):
            with self.subTest(length=len(packed)),self.assertRaises(ValueError):audit.decode_candidate_packet(base64.b64encode(packed).decode(),self.binding)
        with self.assertRaises(ValueError):audit.decode_candidate_packet('x'*45001,self.binding)
        with self.assertRaises(ValueError):audit.decode_candidate_packet(packet,{**self.binding,'sha256':'0'*64})
    def test_raw_loader_keeps_conflict_at_tick_not_whole_market(self):
        self.raw.append(quote(self.row['slug'],170,bid=71,size=1));self.write_quotes()
        source,manifest=self.load_source();key=self.row['market_id'];book=source.books[key]
        self.assertEqual(len(book),3);self.assertTrue(book.loc[book.ts_utc.eq(START+pd.Timedelta(seconds=170)),'quote_conflict'].iloc[0])
        selected,reason=source.select(key,START+pd.Timedelta(seconds=132),START+pd.Timedelta(seconds=300),side='up',kind='entry',max_quote_delay_seconds=3.)
        self.assertEqual(reason,'selected')
        _,reason=source.select(key,START+pd.Timedelta(seconds=170),START+pd.Timedelta(seconds=300),side='up',kind='exit',max_quote_delay_seconds=3.)
        self.assertEqual(reason,'unknown_quote_conflict');self.assertEqual(manifest['files'][0]['sha256'],audit.digest(self.quote_path.read_bytes()))
        conflict=next(r for r in source.witness if r['quote']['quote_conflict']);self.assertEqual(len(conflict['quote']['conflict_evidence']),2)
    def test_only_causal_window_plus_presence_anchor_retained(self):
        self.raw=[quote(self.row['slug'],0),quote(self.row['slug'],100),quote(self.row['slug'],250)];self.write_quotes()
        source,manifest=self.load_source();self.assertEqual(len(source.books[self.row['market_id']]),1)
        _,reason=source.select(self.row['market_id'],START+pd.Timedelta(seconds=132),START+pd.Timedelta(seconds=300),side='up',kind='entry',max_quote_delay_seconds=3.)
        self.assertEqual(reason,'no_quote_in_window');self.assertEqual(manifest['retained_rows'],0)
    def test_retention_never_clips_original_window_for_short_retry_deadline(self):
        self.raw=[quote(self.row['slug'],135),quote(self.row['slug'],175,size=0)];self.write_quotes()
        source,_=audit.load_candidate_quote_books(self.source,self.load_candidates(),self.parent,self.config['raw_budget'],deadline_after_original_target_seconds=1.)
        _,reason=source.select(self.row['market_id'],START+pd.Timedelta(seconds=173),START+pd.Timedelta(seconds=300),side='up',kind='exit',max_quote_delay_seconds=3.)
        self.assertEqual(reason,'selected')

    def test_raw_missing_schema_clock_scope_and_budget_are_input_failures(self):
        for mutation in ('missing_run','file_count','schema','clock','rowbudget','bytebudget','outside_scope'):
            with self.subTest(mutation=mutation):
                original=self.quote_path.read_bytes();old=copy.deepcopy(self.config);parent=copy.deepcopy(self.parent)
                try:
                    if mutation=='missing_run':self.quote_path.unlink();(self.source/'run').rmdir()
                    elif mutation=='file_count':self.parent['expected_files_per_run']=2
                    elif mutation=='schema':self.quote_path.write_text('slug,ts_iso\nabc,bad\n')
                    elif mutation=='clock':self.raw[0]['ts_iso']='unknown';self.write_quotes()
                    elif mutation=='rowbudget':self.config['raw_budget']['max_quote_rows']=1
                    elif mutation=='bytebudget':self.config['raw_budget']['max_quote_bytes']=1
                    else:self.parent['run_order']=['past','other']
                    with self.assertRaises(ValueError):self.load_source()
                finally:
                    (self.source/'run').mkdir(exist_ok=True);self.quote_path.write_bytes(original);self.config=old;self.parent=parent;self.raw[0]['ts_iso']=(START+pd.Timedelta(seconds=132)).isoformat()
    def test_symlink_candidate_source_and_metadata_changed_refused(self):
        link=self.root/'link.csv';link.symlink_to(self.candidate_path)
        with self.assertRaises(OSError):audit.load_frozen_candidates(link,self.binding,[self.row])
        raw=self.quote_path.read_bytes();self.quote_path.unlink();self.quote_path.symlink_to(self.candidate_path)
        with self.assertRaises((ValueError,OSError)):self.load_source()
        self.quote_path.unlink();self.quote_path.write_bytes(raw)
        with patch.object(audit,'stable',return_value=False):
            with self.assertRaisesRegex(ValueError,'changed'):self.load_candidates()
    def test_two_actual_synthetic_replays_accept_losing_exit_model_free(self):
        expected=self.baseline();self.config['expected_original_pooled_fingerprint']=audit.fingerprints.result_fingerprint(expected)
        original=audit.execution.replay_cached_quotes
        with patch.object(audit,'verified_code_source',return_value={}),patch.object(audit.execution,'replay_cached_quotes',wraps=original) as calls, \
                patch.object(audit.book_base,'Ridge',side_effect=AssertionError('no model construction')), \
                patch.object(audit.book_base,'prepare_archive',side_effect=AssertionError('no feature regeneration')):
            result=audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
        self.assertEqual(calls.call_count,2);self.assertNotIn('exit_resolver',calls.call_args_list[0].kwargs)
        self.assertEqual(result['arms']['C_original'],expected);self.assertTrue(result['strategy_evaluated']);self.assertTrue(result['comparison_valid'])
        retry=result['arms']['C_scheduled_retry'];self.assertEqual(retry['chain'][0]['exit_ts'],(START+pd.Timedelta(seconds=182)).isoformat())
        self.assertLess(retry['chain'][0]['realized_pnl'],0);self.assertEqual(result['actual_model_fit_calls'],0);self.assertEqual(result['replayed_cells'],2)
        self.assertFalse(result['economic_pass']['C_scheduled_retry']);self.assertEqual({p.name for p in (self.root/'out').iterdir()},audit.OUTPUTS)
    def test_attempt_evidence_includes_unchanged_original_success_and_no_quote(self):
        for case in ('full','no_quote'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                self.raw=[quote(self.row['slug'],132),quote(self.row['slug'],170)] if case=='full' else [quote(self.row['slug'],132)]
                self.write_quotes();self.config['expected_original_pooled_fingerprint']=audit.fingerprints.result_fingerprint(self.baseline())
                with patch.object(audit,'verified_code_source',return_value={}):
                    report=audit.evaluate(self.source,self.candidate_path,self.config,Path(folder)/'out')
                ledger=report['arms']['C_scheduled_retry'];self.assertNotIn('exit_retry',ledger['chain'][0])
                rows=[json.loads(line) for line in (Path(folder)/'out/exit-attempts.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['attempt_index'],0)
                self.assertFalse(rows[0]['is_retry']);self.assertEqual(rows[0]['status'],'full_size' if case=='full' else 'no_quote_in_window')

    def test_unknown_exit_size_stays_locked_and_all_artifacts_strict_json(self):
        self.raw=[quote(self.row['slug'],132),quote(self.row['slug'],170,size=float('nan'))];self.write_quotes()
        report=self.evaluate()
        self.assertTrue(report['comparison_valid']);self.assertEqual(report['replayed_cells'],2)
        for arm in audit.ARMS:
            ledger=report['arms'][arm];self.assertEqual(ledger['chain'][0]['status'],'unresolved_position')
            self.assertGreater(ledger['locked_shares'],0);self.assertIsNone(ledger['chain'][0]['exit_quote_evidence']['available_shares'])
        for path in (self.root/'out').iterdir():
            if path.suffix=='.jsonl':
                for line in path.read_bytes().splitlines():audit.load_json(line)
            else:audit.load_json(path.read_bytes())
        self.assertEqual(audit.json_safe({'unknown':float('nan'),'negative_infinite':float('-inf')}),{'unknown':None,'negative_infinite':None})
        with self.assertRaisesRegex(ValueError,'nonfinite'):audit.fingerprints.result_fingerprint({'unknown':float('nan')})

    def test_full_baseline_guard_stops_before_retry_and_keeps_evidence(self):
        original=audit.execution.replay_cached_quotes
        with patch.object(audit,'verified_code_source',return_value={}),patch.object(audit.execution,'replay_cached_quotes',wraps=original) as calls:
            with self.assertRaisesRegex(ValueError,'evaluation_failed'):audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
        self.assertEqual(calls.call_count,1);result=json.loads((self.root/'out/summary.json').read_text())
        self.assertEqual(result['replayed_cells'],1);self.assertFalse(result['comparison_valid']);self.assertFalse((self.root/'out/retry-ledger.json').exists())
        self.assertTrue((self.root/'out/baseline-ledger.json').exists());self.assertEqual(result['failure']['code'],'baseline_full_fingerprint_mismatch')
    def test_invalid_config_precedes_raw_read_and_keeps_failure(self):
        self.config['actual_model_fit_calls']=True
        with patch.object(audit,'load_candidate_quote_books',side_effect=AssertionError('must not read')) as loaded:
            with self.assertRaisesRegex(ValueError,'evaluation_failed'):audit.evaluate(self.source,self.candidate_path,self.config,self.root/'out')
        self.assertEqual(loaded.call_count,0);self.assertEqual(json.loads((self.root/'out/summary.json').read_text())['replayed_cells'],0)
    def test_config_clock_budget_hash_and_two_replay_contracts(self):
        audit.validate_config(self.config)
        for key,value in [('max_producer_replays',3),('base_protocol_sha256','0'*64),('live_orders_enabled',True)]:
            with self.subTest(key=key):
                wrong=copy.deepcopy(self.config);wrong[key]=value
                with self.assertRaises(ValueError):audit.validate_config(wrong)
        wrong=copy.deepcopy(self.config);wrong['retry_policy']['order_latency_seconds']=0
        with self.assertRaises(ValueError):audit.validate_config(wrong)
        wrong=copy.deepcopy(self.config);wrong['raw_budget']['max_quote_rows']=audit.HARD_RAW['max_quote_rows']+1
        with self.assertRaises(ValueError):audit.validate_config(wrong)
    def test_cash_readout_complete_ten_and_missing_clocks_not_fast_claim(self):
        chain=[]
        for i in range(10):
            entry=START+pd.Timedelta(seconds=i*1300)
            chain.append({'market_id':str(i),'entry_ts':entry.isoformat(),'exit_ts':(entry+pd.Timedelta(seconds=40)).isoformat(),
                'cash_release_ts':(entry+pd.Timedelta(seconds=1000)).isoformat(),'status':'timed_quote_exit'})
        ledger={'chain':chain,'locked_shares':0.,'ending_cash':20.}
        result=audit.cash_time_readout(ledger,self.parent)
        self.assertTrue(result['complete_first_ten_resolved']);self.assertEqual(result['first_ten_cash_multiple'],2.)
        self.assertEqual(result['elapsed_first_entry_to_tenth_assumed_release_seconds'],12700.)
        self.assertEqual(result['positions'][1]['idle_after_previous_assumed_release_seconds'],300.)
        self.assertNotIn('APY',result);chain[-1].pop('cash_release_ts')
        result=audit.cash_time_readout(ledger,self.parent);self.assertIsNone(result['elapsed_first_entry_to_tenth_assumed_release_seconds'])
        self.assertEqual(result['elapsed_status'],'incomplete_or_unknown')
    def test_cash_readout_nanosecond_idle_not_rounded_to_zero(self):
        first={'market_id':'one','entry_ts':START.isoformat(),'exit_ts':(START+pd.Timedelta(seconds=1)).isoformat(),
            'cash_release_ts':(START+pd.Timedelta(seconds=2)).isoformat(),'status':'timed_quote_exit'}
        second={'market_id':'two','entry_ts':(START+pd.Timedelta(seconds=2,nanoseconds=1)).isoformat(),'status':'unresolved_position'}
        result=audit.cash_time_readout({'chain':[first,second],'ending_cash':0.,'locked_shares':1.},self.parent)
        self.assertEqual(result['positions'][1]['idle_after_previous_assumed_release_seconds'],1e-9)

    def test_cash_readout_overlap_conflict_and_locked_cash_not_wealth(self):
        e={'market_id':'one','entry_ts':START.isoformat(),'exit_ts':(START+pd.Timedelta(seconds=40)).isoformat(),
            'cash_release_ts':(START+pd.Timedelta(seconds=1000)).isoformat(),'status':'timed_quote_exit'}
        ledger={'chain':[e,{**e,'market_id':'two'}],'ending_cash':0.,'locked_shares':2.}
        with self.assertRaisesRegex(ValueError,'overlap'):audit.cash_time_readout(ledger,self.parent)
        ledger['chain']=[{'market_id':'one','entry_ts':START.isoformat(),'status':'unresolved_position'}]
        result=audit.cash_time_readout(ledger,self.parent);self.assertEqual(result['ending_liquid_cash'],0.);self.assertEqual(result['locked_shares'],2.)
        self.assertIsNone(result['elapsed_first_entry_to_tenth_assumed_release_seconds']);self.assertNotIn('total_wealth',result)

if __name__=='__main__':unittest.main()
