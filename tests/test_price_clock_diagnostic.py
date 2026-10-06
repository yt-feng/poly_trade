import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analysis import price_clock_diagnostic as state
from analysis import historical_walk_forward as walk
from analysis import archive_coverage_ledger as ledger
from analysis import quote_robustness as robustness
from tests import test_spread_hurdle_diagnostic as fixture
from tests.test_archive_coverage_ledger import DATE, START
from tests.test_historical_walk_forward import quote, config


def quotes(start=START,shift=0):
    out=[]
    for offset in (60,120,240):
        for delta,level in ((-16,40+shift),(-1,42+shift),(1,43+shift),(30,45+shift)):
            q=quote(start,offset+delta,level)
            q['values']=q['values'][:2]+(72+shift,70+shift)+q['values'][4:]
            out.append(q)
    return sorted(out,key=lambda q:q['ts'])


def sample(offset=120,start=START,date=DATE,rows=None):
    return state.sample(quotes(start) if rows is None else rows,f'btc-updown-5m-{start}',start,date,offset,config())


def const(value):return {'fields':[],'intercept':value,'center':[],'scale':[],'coefficients':[]}


def fits():
    return {side:{t:{n:const(6 if side=='up' else 7) for n in state.MODELS} for t in state.TARGETS} for side in ('up','down')}


def legacy():return {t:{'all':const(3),'zero':const(0)} for t in state.TARGETS}


class StateTests(unittest.TestCase):
    def test_state_has_boundary_and_clock_variation_without_resolution(self):
        c=quote(START,119,49.5);l=quote(START,104,48.5)
        a=state.state_features(c,l,'up',60,30);b=state.state_features(c,l,'up',240,30)
        self.assertEqual(a['level'],0)
        self.assertEqual(a['level_clock'],0)
        self.assertEqual(b['momentum_boundary_clock'],4*a['momentum_boundary_clock'])
        c=quote(START,119,98);l=quote(START,104,97)
        edge=state.state_features(c,l,'up',60,30)
        self.assertLess(edge['momentum_boundary_clock'],a['momentum_boundary_clock'])

    def test_direct_down_target_and_features_are_not_up_complements(self):
        r=sample()
        self.assertEqual(r['current_prices']['down'],{'ask':72,'bid':70})
        self.assertEqual(r['side_targets']['down']['bid_change_cents'],0)
        self.assertEqual(r['side_targets']['up']['bid_change_cents'],3)
        self.assertNotEqual(r['state_features']['down']['level'],-r['state_features']['up']['level'])

    def test_future_and_unavailable_official_fields_cannot_change_features(self):
        raw=quotes();before=sample(rows=raw)
        for q in raw:
            q['official_open']=1e9;q['resolution_label']=1;q['final_price']=9e9
            if q['ts']>=START+150:q['values']=(90,89)+q['values'][2:]
        after=sample(rows=raw)
        self.assertEqual(before['state_features'],after['state_features'])
        self.assertNotEqual(before['side_targets'],after['side_targets'])

    def test_first_future_invalid_not_skipped(self):
        raw=quotes()
        for q in raw:
            if q['ts']==START+150:q['price_reason']='missing_price'
        raw.append(quote(START,151,60));raw.sort(key=lambda q:q['ts'])
        r=sample(rows=raw)
        self.assertTrue(r['feature_eligible']);self.assertIsNone(r['side_targets']['up'])
        self.assertFalse(r['paths']['up']['0']['price_only']['known'])

    def test_delayed_entry_real_ask_fixed_exit_and_bad_record_unknown(self):
        r=sample()
        self.assertEqual(r['paths']['up']['1']['ask'],44)
        self.assertEqual(r['paths']['up']['1']['bid'],45)
        self.assertEqual(r['paths']['up']['1']['entry_delay_recorded_seconds'],1)
        self.assertEqual(r['observed_exit_time'],START+150)
        raw=quotes()
        for q in raw:
            if q['ts']==START+121:q['price_reason']='bad_ask'
        r=sample(rows=raw)
        self.assertFalse(r['paths']['up']['1']['price_only']['known'])
        self.assertIsNone(r['paths']['up']['1']['ask'])

    def test_late_entry_and_nonpositive_depth_unknown(self):
        raw=[q for q in quotes() if q['ts']!=START+121]
        r=sample(rows=raw)
        self.assertIn('entry_at_or_after_fixed_exit',r['paths']['up']['1']['price_only']['unknown_reasons'])
        raw=quotes()
        for q in raw:
            if q['ts']==START+121:q['values']=q['values'][:4]+(0,)+q['values'][5:]
        r=sample(rows=raw)
        self.assertTrue(r['paths']['up']['1']['price_only']['known'])
        self.assertFalse(r['paths']['up']['1']['positive_depth']['known'])

    def test_duplicate_cross_window_and_wrong_identity_fail(self):
        for raw in (quotes()+[quotes()[0]],quotes()+[quote(START,301,45)]):
            with self.assertRaisesRegex(ValueError,'duplicate_or_cross_window'):sample(rows=raw)
        with self.assertRaisesRegex(ValueError,'invalid_market_identity'):
            state.sample(quotes(),'another-market',START,DATE,120,config())

    def test_training_requires_all_states_of_whole_market(self):
        rows=[sample(o) for o in (60,120,240)]
        good,c=state.training_rows(rows,{DATE},[60,120,240],START+86400)
        self.assertEqual((len(good),c['complete_markets']),(3,1))
        rows[2]['side_targets']['up']=None
        good,c=state.training_rows(rows,{DATE},[60,120,240],START+86400)
        self.assertEqual((len(good),c['excluded_incomplete_markets']),(0,1))
        with self.assertRaisesRegex(ValueError,'missing_training_state'):
            state.training_rows(rows[:2],{DATE},[60,120,240],START+86400)

    def test_future_training_label_is_rejected(self):
        rows=[sample(o) for o in (60,120,240)];rows[0]['target_time']=START+86400
        with self.assertRaisesRegex(ValueError,'after_cutoff'):
            state.training_rows(rows,{DATE},[60,120,240],START+86400)

    def test_train_only_fits_do_not_depend_on_validation_targets(self):
        rows=[sample(o) for o in (60,120,240)]
        val=[sample(o,START+86400,'2026-09-27') for o in (60,120,240)]
        train,_=state.training_rows(rows+val,{DATE},[60,120,240],START+86400)
        first=state.fit_new(train,1)
        for r in val:
            r['side_targets']['up']['bid_change_cents']=99;r['state_features']['up']['level']=900
        train,_=state.training_rows(rows+val,{DATE},[60,120,240],START+86400)
        self.assertEqual(state.fit_new(train,1),first)

    def test_projection_respects_actual_side_quote_bounds(self):
        raw,point=state.predict_new(const(40),{}, {'bid':97,'ask':98},'bid_change_cents')
        self.assertEqual((raw,point),(40,3))
        raw,point=state.predict_new(const(-40),{}, {'bid':2,'ask':3},'ask_change_cents')
        self.assertEqual((raw,point),(-40,-3))

    def test_new_gate_has_fixed_cost_buffer_legacy_gate_unchanged(self):
        r=sample();old=legacy();saved=copy.deepcopy(old)
        new=fits();new['up']['bid_change_cents']['level']=const(2)
        state.populate(r,new,old,1)
        self.assertFalse(r['signals']['up']['state_level'])  # equality doesn't pass
        self.assertTrue(r['signals']['up']['legacy_all'])
        self.assertTrue(r['signals']['down']['state_level']) # actual Down model
        self.assertFalse(r['signals']['down']['legacy_all']) # old sign reversal remains
        self.assertEqual(old,saved)
        a=robustness.summarize([r],list(r['predictions']['up']['bid_change_cents']),'up','1','price_only',fixture.ZERO)
        b=robustness.summarize([r],list(r['predictions']['up']['bid_change_cents']),'up','1','price_only',fixture.COST)
        self.assertEqual(a['policies']['state_clock']['selected'],b['policies']['state_clock']['selected'])

    def test_unknowns_and_no_trade_keep_same_sample_denominator(self):
        rows=[sample(),sample(start=START+300)]
        for r in rows:state.populate(r,fits(),legacy(),1)
        rows[1]['paths']['up']['1']['positive_depth'].update(known=False,unknown_reasons=['unknown'])
        a=state.primary_fold_economics(rows,['state_clock'],'up')
        self.assertEqual((a['known'],a['unknown']),(1,1))
        self.assertEqual(a['policies']['state_clock']['unknown'],1)
        self.assertEqual(a['policies']['no_signal']['selected'],0)
        self.assertEqual(a['policies']['no_signal']['shared_activity_mean'],0)

    def test_missing_fit_does_not_invent_predictions(self):
        r=sample();state.populate(r,{},legacy(),1)
        self.assertFalse(r['eligible']);self.assertTrue(r['feature_eligible'])
        self.assertEqual(r['predictions'],{})

    def test_actual_cli_keeps_legacy_fits_and_all_states_in_split(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg=config();grouped={}
            for day in (0,1):
                for i in range(3):
                    start=START+day*86400+i*300
                    grouped[f'btc-updown-5m-{start}']=quotes(start,i)
            fixture.write_observations(root,grouped);database=root/'coverage.sqlite';ledger.build(root,database)
            folds=[{'train_dates':[DATE],'validation_dates':['2026-09-27']}]
            with ledger.connect_readonly(database) as db:
                samples=walk.samples_for_dates(db,[DATE,'2026-09-27'],cfg,[0,20,40,60,80,100])
            old_plan={'diagnostic':cfg,'folds':folds,'quote_bin_edges_cents':[0,20,40,60,80,100]}
            old={'plan':old_plan,'cohorts':{'depth':walk.evaluate(samples['depth'],folds,cfg,'depth')}}
            source=root/'legacy.json';source.write_text(json.dumps(old));saved=source.read_bytes()
            costs=[fixture.ZERO,dict(fixture.ZERO,id='assumed_fee',fee_cents_per_share_per_leg=.5),
                   dict(fixture.COST,id='assumed_fee_slippage')]
            p={'decision_offsets':[60,120,240],'new_models':state.MODELS,'entry_steps':[0,1],'new_signal_buffer_cents':1,
               'training_policy':'all_states_labeled_complete_market','selection':'all_registered_no_validation_selection',
               'prior_plan':old_plan,'folds':folds,'cost_scenarios':costs,'minimum_training_markets':1,
               'database_sha256':walk.file_hash(database),'legacy_file_sha256':walk.file_hash(source),'code_sha256':{}}
            protocol=root/'plan.json';protocol.write_text(json.dumps(p));out=root/'result.json'
            process=subprocess.run([sys.executable,'-m','analysis.price_clock_diagnostic','--database',str(database),
                '--poly-root',str(root),'--legacy',str(source),'--protocol',str(protocol),'--output',str(out)],capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            r=json.loads(out.read_text());self.assertEqual(source.read_bytes(),saved)
            self.assertFalse(r['legacy_refit']);self.assertIsNone(r['pnl'])
            self.assertEqual(out.stat().st_mode & 0o777,0o600)
            self.assertEqual(len(r['window_ledger']),288*3)
            self.assertEqual(r['folds'][0]['training']['complete_markets'],3)
            self.assertEqual(set(x['date'] for x in r['window_ledger']),{'2026-09-27'})
            self.assertFalse(r['screen']['up']['state_level']['passes_all_registered_states'])
            p['folds'][0]['train_dates']=['2026-09-27']
            with self.assertRaises(ValueError):state.validate_plan(p)


if __name__=='__main__':unittest.main()
