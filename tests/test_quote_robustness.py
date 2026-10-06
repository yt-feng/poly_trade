import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analysis import quote_robustness as robust
from analysis import spread_hurdle_diagnostic as hurdle
from analysis import archive_coverage_ledger as ledger
from analysis import historical_walk_forward as walk
from tests import test_spread_hurdle_diagnostic as fixture
from tests.test_archive_coverage_ledger import DATE, START
from tests.test_historical_walk_forward import quote, config


def plan():
    return {'entry_steps':[0,1,3], 'block_hours':6, 'top_counts':[1,3,5],
            'exit_policy':'original_scheduled_exit_first_record_no_shift',
            'selection':'all_prior_candidates_no_refit_or_reselection',
            'quantiles':[.01,.05,.25,.5,.75,.95,.99], 'views':['price_only','positive_depth'],
            'step_origin':'strictly_after_decision_actual_records_no_invalid_skip',
            'prior_hurdle_protocol':fixture.plan()}


def simple_row(start=START, ask=43, bid=46, active=True, eligible=True):
    path = {'ask':ask,'bid':bid,'entry_delay_recorded_seconds':1,
            'price_only':{'known':bid is not None and ask is not None, 'unknown_reasons':[] if bid is not None else ['exit_missing']},
            'positive_depth':{'known':bid is not None and ask is not None, 'unknown_reasons':[] if bid is not None else ['exit_missing']}}
    return {'market':f'm{start}', 'start':start, 'date':datetime.fromtimestamp(start,timezone.utc).date().isoformat(),
            'eligible':eligible,'signals':{'up':{'candidate':active,'always_same_side':True,'no_signal':False}},
            'paths':{'up':{str(i):copy.deepcopy(path) for i in (0,1,3)}}}


class RobustnessTests(unittest.TestCase):
    def attach_fixture(self, root, middle=None):
        cfg=config(); raw=[quote(START,104,49),quote(START,119,50)]
        raw += middle if middle is not None else [quote(START,121,51),quote(START,123,52),quote(START,127,54)]
        raw += [quote(START,150,52),quote(START,155,90)]
        market=f'btc-updown-5m-{START}'; fixture.write_observations(root,{market:raw})
        database=root/'coverage.sqlite';ledger.build(root,database)
        sample=walk.sample_window(raw,market,START,DATE,cfg,'common',[0,100],True)
        sample.update(fold=0,predictions={'bid_change_cents':{'candidate':3},'ask_change_cents':{'candidate':-3}})
        with ledger.connect_readonly(database) as db:
            previous=hurdle.attach_path(db,sample,cfg)
        return database,sample,previous,cfg

    def test_actual_steps_keep_exit_and_signal_frozen(self):
        with tempfile.TemporaryDirectory() as d:
            database,old,prior,cfg=self.attach_fixture(Path(d))
            with ledger.connect_readonly(database) as db:
                r=robust.attach(db,old,prior,cfg)
            self.assertEqual([r['paths']['up'][str(i)]['entry_delay_recorded_seconds'] for i in (0,1,3)],[-1,1,7])
            self.assertEqual([r['paths']['up'][str(i)]['ask'] for i in (0,1,3)],[51,52,55])
            self.assertEqual([r['paths']['up'][str(i)]['bid'] for i in (0,1,3)],[52]*3)
            self.assertEqual(r['fixed_exit_record']['ts_us'],(START+150)*1000000)
            self.assertTrue(r['signals']['up']['candidate'])
            self.assertEqual(r['paths']['down']['3']['ask'],50)  # actual Down, not 100-Up

    def test_invalid_delayed_record_not_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            bad=quote(START,121,51);bad['values']=(None,)+bad['values'][1:]
            database,old,prior,cfg=self.attach_fixture(Path(d),[bad,quote(START,122,55),quote(START,124,56)])
            with ledger.connect_readonly(database) as db:r=robust.attach(db,old,prior,cfg)
            self.assertFalse(r['paths']['up']['1']['price_only']['known'])
            self.assertIsNone(r['paths']['up']['1']['ask'])
            self.assertEqual(r['paths']['up']['1']['entry_record']['ts_us'],(START+121)*1000000)
            self.assertTrue(r['paths']['up']['3']['price_only']['known'])

    def test_entry_at_exit_is_unknown_and_exit_never_shifted(self):
        with tempfile.TemporaryDirectory() as d:
            database,old,prior,cfg=self.attach_fixture(Path(d),[])
            with ledger.connect_readonly(database) as db:r=robust.attach(db,old,prior,cfg)
            self.assertEqual(r['paths']['up']['1']['price_only']['unknown_reasons'],['entry_at_or_after_fixed_exit'])
            self.assertEqual(r['paths']['up']['3']['price_only']['unknown_reasons'],['entry_missing'])
            self.assertEqual(r['fixed_exit_record']['ts_us'],(START+150)*1000000)

    def test_nonpositive_depth_is_unknown_only_in_depth_view(self):
        with tempfile.TemporaryDirectory() as d:
            database,old,prior,cfg=self.attach_fixture(Path(d))
            with sqlite3.connect(database) as db:
                db.execute('UPDATE observations SET sizes=? WHERE ts_us=?',('[0,10,10,10]',(START+121)*1000000))
            with ledger.connect_readonly(database) as db:r=robust.attach(db,old,prior,cfg)
            p=r['paths']['up']['1']
            self.assertTrue(p['price_only']['known']); self.assertFalse(p['positive_depth']['known'])
            self.assertIn('entry_nonpositive_depth',p['positive_depth']['unknown_reasons'])
            self.assertEqual(robust.bounds(p,'positive_depth',2),(-102,98))

    def test_last_decision_record_required_not_earlier_valid_one(self):
        with tempfile.TemporaryDirectory() as d:
            database,old,prior,cfg=self.attach_fixture(Path(d))
            with sqlite3.connect(database) as db:
                db.execute('INSERT INTO observations (slug,ts_us,start,prices,sizes) VALUES (?,?,?,?,?)',
                    (old['market'],round((START+119.5)*1e6),START,'[80,79,60,59]','[10,10,10,10]'))
            with ledger.connect_readonly(database) as db:
                with self.assertRaisesRegex(ValueError,'last_causal_record'):robust.attach(db,old,prior,cfg)

    def test_cross_window_delay_stops_not_silently_removed(self):
        with tempfile.TemporaryDirectory() as d:
            database,old,prior,cfg=self.attach_fixture(Path(d))
            with sqlite3.connect(database) as db:
                db.execute('UPDATE observations SET start=? WHERE ts_us=?',(START+300,(START+121)*1000000))
            with ledger.connect_readonly(database) as db:
                with self.assertRaisesRegex(ValueError,'cross_window'):robust.attach(db,old,prior,cfg)

    def test_conflicting_duplicate_and_crossed_spread_checked_from_values(self):
        with tempfile.TemporaryDirectory() as d:
            database,old,prior,cfg=self.attach_fixture(Path(d))
            with sqlite3.connect(database) as db:
                db.execute('UPDATE observations SET price_conflict=1 WHERE ts_us=?',((START+121)*1000000,))
                db.execute('UPDATE observations SET prices=? WHERE ts_us=?',('[40,50,60,59]',(START+127)*1000000))
            with ledger.connect_readonly(database) as db:r=robust.attach(db,old,prior,cfg)
            self.assertIn('duplicate_price_conflict',r['paths']['up']['1']['price_only']['unknown_reasons'])
            self.assertIn('crossed_spread',r['paths']['up']['3']['price_only']['unknown_reasons'])

    def test_costs_do_not_change_selection_and_unknown_stays_in_denominator(self):
        rows=[simple_row(),simple_row(START+300,bid=None),simple_row(START+600,active=False)]
        z=robust.summarize(rows,['candidate'],'up','0','price_only',fixture.ZERO)
        c=robust.summarize(rows,['candidate'],'up','0','price_only',fixture.COST)
        self.assertEqual((z['known'],z['unknown']),(2,1))
        a=z['policies']['candidate'];b=c['policies']['candidate']
        self.assertEqual((a['selected'],a['unknown_selected']),(2,1))
        self.assertEqual(a['selected'],b['selected'])
        self.assertEqual(a['selected_lower'],-20)
        self.assertEqual(a['observed_distribution']['mean']-b['observed_distribution']['mean'],2)

    def test_all_delay_intersection_and_all_candidate_baselines_share_sample(self):
        rows=[simple_row(),simple_row(START+300,bid=40,active=False)]
        rows[1]['paths']['up']['3']['price_only'].update(known=False,unknown_reasons=['entry_missing'])
        rows[1]['paths']['up']['3']['ask']=None
        z=robust.summarize(rows,['candidate'],'up','0','price_only',fixture.ZERO)
        t=robust.summarize(rows,['candidate'],'up','3','price_only',fixture.ZERO)
        self.assertEqual((z['known'],t['known'],z['shared_all_delay_known'],t['shared_all_delay_known']),(2,1,1,1))
        a=z['policies']['candidate']
        self.assertEqual(a['shared_activity_mean'],1.5)
        self.assertEqual(a['paired_same_sample_vs']['always_same_side'],1.5)
        self.assertEqual(a['all_delay_same_sample_activity_mean'],3)

    def test_leave_day_block_out_preserves_fixed_values_and_unknowns(self):
        rows=[simple_row(bid=63),simple_row(START+86400,bid=40),simple_row(START+86700,bid=None)]
        z=robust.summarize(rows,['candidate'],'up','0','price_only',fixture.ZERO)['policies']['candidate']
        groups=z['leave_one_out_no_refit']['day']['groups']
        self.assertEqual(groups[DATE]['retained_selected_mean'],-3)
        self.assertEqual(groups[DATE]['retained_selected_lower'],-23)
        self.assertEqual(z['leave_one_out_no_refit']['day']['min_retained_mean'],-3)
        self.assertEqual(z['leave_one_out_no_refit']['six_hour']['min_retained_mean'],-3)

    def test_median_tail_and_contribution_denominators(self):
        x=[-3,-2,-1,20]
        self.assertEqual(robust.distribution(x)['median'],-1.5)
        c=robust.concentration(x)['top']['1']
        self.assertEqual(c['positive_share_of_gross_positive'],1)
        self.assertAlmostEqual(c['absolute_share'],20/26)
        self.assertAlmostEqual(c['positive_to_net_ratio'],20/14)
        self.assertEqual(c['remaining_mean_after_removing_largest_positive'],-2)
        self.assertIsNone(robust.distribution([])['mean'])

    def test_unobservable_calendar_and_no_signal_are_preserved(self):
        rows=[simple_row(),simple_row(START+86400,eligible=False)]
        z=robust.summarize(rows,['candidate'],'up','0','price_only',fixture.ZERO)
        self.assertEqual((z['scheduled'],z['eligible'],z['unobservable_signal']),(2,1,1))
        self.assertEqual(z['policies']['no_signal']['shared_activity_mean'],0)
        self.assertIn(rows[1]['date'],z['policies']['candidate']['leave_one_out_no_refit']['day']['groups'])

    def test_protocol_rejects_retuning_and_shifted_exit(self):
        for key,value in [('entry_steps',[0,2]),('exit_policy','entry_plus_horizon'),('selection','best_candidate')]:
            p=plan();p[key]=value
            with self.assertRaisesRegex(ValueError,'unregistered'):robust.validate_plan(p)

    def test_real_cli_no_fit_and_duplicate_market_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg=config();grouped={}
            for day in (0,1):
                for i in range(3):
                    start=START+day*86400+i*300
                    grouped[f'btc-updown-5m-{start}']=[quote(start,104,30+i),quote(start,119,31+2*i),
                        quote(start,121,32+2*i),quote(start,123,33+2*i),quote(start,126,34+2*i),quote(start,150,32+3*i)]
            fixture.write_observations(root,grouped)
            database=root/'coverage.sqlite';ledger.build(root,database)
            folds=[{'train_dates':[DATE],'validation_dates':['2026-09-27']}]
            with ledger.connect_readonly(database) as db:samples=walk.samples_for_dates(db,[DATE,'2026-09-27'],cfg,[0,100])
            cohorts={c:walk.evaluate(rows,folds,cfg,c) for c,rows in samples.items()}
            old={'plan':{'diagnostic':cfg,'database_sha256':hurdle.file_hash(database)},'protocol_sha256':'synthetic','cohorts':cohorts}
            source=root/'previous.json';source.write_text(json.dumps(old))
            hp=fixture.plan();hp.update(database_sha256=hurdle.file_hash(database),prediction_file_sha256=hurdle.file_hash(source),
                prior_protocol_sha256='synthetic',code_sha256={},cohort_models={c:v['models'] for c,v in cohorts.items()},
                cohort_scheduled_windows={c:len(v['window_ledger']) for c,v in cohorts.items()})
            prior=hurdle.run(database,root,source,hp);prior['protocol_sha256']='synthetic-hurdle'
            previous=root/'hurdle.json';previous.write_text(json.dumps(prior))
            p=plan();p.update(prior_hurdle_protocol=hp,database_sha256=hurdle.file_hash(database),prediction_sha256=hurdle.file_hash(source),
                prior_report_sha256=hurdle.file_hash(previous),prior_protocol_sha256='synthetic-hurdle',code_sha256={})
            protocol=root/'plan.json';protocol.write_text(json.dumps(p));output=root/'result.json'
            with patch('analysis.quote_factor_diagnostic.fit',side_effect=AssertionError('refit forbidden')):
                r=robust.run(database,root,source,previous,p)
            self.assertFalse(r['models_refit']);self.assertFalse(r['signals_reselected']);self.assertIsNone(r['pnl'])
            process=subprocess.run([sys.executable,'-m','analysis.quote_robustness','--database',str(database),'--poly-root',str(root),
                '--predictions',str(source),'--prior-report',str(previous),'--protocol',str(protocol),'--output',str(output)],capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual(output.stat().st_mode & 0o777,0o600)
            self.assertIn('PRIVATE_ROBUSTNESS_COMPLETE',process.stdout)
            old['cohorts']['common']['window_ledger'][1]=old['cohorts']['common']['window_ledger'][0]
            source.write_text(json.dumps(old));p['prediction_sha256']=hurdle.file_hash(source)
            with self.assertRaisesRegex(ValueError,'duplicate_validation_market'):robust.run(database,root,source,previous,p)


if __name__=='__main__':unittest.main()
