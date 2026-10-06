from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis import recorded_price_bounds as bounds
from analysis import quote_measurement_audit as measurement
from analysis import price_clock_diagnostic as state
from tests import test_price_clock_diagnostic as fixture
from tests.test_historical_walk_forward import config, quote
from tests.test_archive_coverage_ledger import DATE, START


def point(price, size=True, reason=None):
    return {'price':price,'positive_size':size,'reason':reason}


class RecordedBoundsTests(unittest.TestCase):
    def test_equality_uses_decimal_and_inclusive_limits(self):
        self.assertEqual(bounds.condition(point(28.01),point(29.01),28.01,'29.01')['status'],'pass')
        self.assertEqual(bounds.condition(point(28.02),point(29.01),28.01,'29.01')['status'],'fail')

    def test_any_valid_price_violation_dominates_missing_other_endpoint(self):
        self.assertEqual(bounds.condition(point(31),point(None,False),30,31)['status'],'fail')
        self.assertEqual(bounds.condition(point(None,False),point(30),30,31)['status'],'fail')
        self.assertEqual(bounds.condition(point(None,False),point(31),30,31)['status'],'unknown')

    def test_zero_size_is_unvalidated_not_filled_or_failed_order(self):
        self.assertEqual(bounds.condition(point(30,False),point(31),30,31)['status'],'unknown')

    def test_impossible_limit_and_invalid_limit(self):
        self.assertEqual(bounds.condition(point(None),point(None),99.5,100.5)['status'],'fail')
        for x in (0,100,float('nan')):
            with self.assertRaises(ValueError):bounds.condition(point(30),point(31),x,101)

    def test_full_book_and_own_field_views_never_synthesize_complement(self):
        r=quote(START,121,29);r['values']=(30,29,None,None,1,1,None,None)
        self.assertEqual(bounds.endpoint(r,0,True,'own_field')['price'],30)
        self.assertIsNone(bounds.endpoint(r,0,True,'full_book')['price'])
        self.assertIsNone(bounds.endpoint(r,2,True,'own_field')['price'])
        r['values']=(30,31,None,None,1,1,None,None)
        self.assertIsNone(bounds.endpoint(r,0,True,'own_field')['price'])

    def test_late_or_nonpositive_price_is_not_evidence(self):
        r=quote(START,121,29)
        self.assertIsNone(bounds.endpoint(r,0,False,'own_field')['price'])
        for x in (0,float('nan'),100,-1):
            r['values']=(x,29,60,59,1,1,1,1)
            self.assertIsNone(bounds.endpoint(r,0,True,'own_field')['price'])

    def test_bad_first_exit_not_replaced_by_later_crossing(self):
        raw=fixture.quotes()
        for r in raw:
            if r['ts']==START+121:r['values']=(42,41,72,70,1,1,1,1)
            if r['ts']==START+150:r['values']=(50,None,72,70,1,None,1,1)
        raw.append(quote(START,151,60));raw.sort(key=lambda r:r['ts'])
        d=measurement.inspect_slot(raw,START,120,config())
        self.assertEqual(bounds.event_for(d,'up','own_field',1)['status'],'unknown')
        self.assertEqual(d['roles']['exit']['ts'],START+150)

    def test_entry_does_not_move_target_and_first_at_target_is_unknown(self):
        raw=[r for r in fixture.quotes() if not START+120<r['ts']<START+150]
        d=measurement.inspect_slot(raw,START,120,config())
        e=bounds.event_for(d,'up','own_field',1)
        self.assertIsNone(e['entry']['price'])
        self.assertEqual(d['target_us'],(START+150)*1000000)

    def test_bounds_keep_selection_missingness_and_empty_cells(self):
        c=Counter()
        for state_name in ('pass','fail','unknown'):
            bounds.count_path(c,True,True,{'status':state_name,'violations':[],'unvalidated':[]})
        bounds.count_path(c,False,False,None);bounds.count_path(c,True,False,None)
        out=bounds.finalize(c)
        self.assertEqual(out['selected_condition_fraction_bounds'],[1/3,2/3])
        self.assertEqual(out['scheduled_selection_and_condition_bounds'],[1/5,3/5])
        self.assertEqual(bounds.finalize(Counter())['selected_condition_fraction_bounds'],[None,None])
        with self.assertRaises(ValueError):bounds.finalize(Counter(planned=2))

    def test_run_retains_full_calendar_and_exact_frozen_decisions(self):
        cfg=config();cfg['models']={'all':[],'zero':[]}
        p={'decision_offsets':[60,120,240],'new_models':state.MODELS,
           'prior_plan':{'diagnostic':cfg},'folds':[{'validation_dates':[DATE]}]}
        fits=fixture.fits();grouped={f'btc-updown-5m-{START}':fixture.quotes()};rows=[]
        for start in range(START,START+86400,300):
            for offset in p['decision_offsets']:
                r=state.sample(grouped.get(f'btc-updown-5m-{start}',[]),f'btc-updown-5m-{start}',start,DATE,offset,cfg,present=start==START)
                state.populate(r,fits,fixture.legacy(),1);r['fold']=0;rows.append(r)
        frozen={'protocol':p,'folds':[{'validation_dates':[DATE],'new_fits':fits}],
                'window_ledger':rows,'summaries':{}}
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'db';db.write_bytes(b'synthetic');f=Path(tmp)/'frozen.json';f.write_text(json.dumps(frozen))
            protocol={'database_sha256':bounds.file_hash(db),'frozen_report_sha256':bounds.file_hash(f),
                      'code_sha256':{},'state_protocol':p,'views':list(bounds.VIEWS),'roundtrip_allowance_cents':1}
            with patch.object(bounds,'connect_readonly',return_value=nullcontext(None)),patch.object(bounds,'inventory',return_value=[]),patch.object(bounds,'verify_sources'),patch.object(bounds,'load_dates',return_value=(grouped,set(),set(),set(grouped))):
                result=bounds.run(db,tmp,f,protocol)
                cell=result['panels']['120/up/own_field/state_level/total/all']
                self.assertEqual((cell['planned'],cell['selection_unavailable'],cell['selected']),(288,287,1))
                self.assertEqual(result['checked_frozen_slots'],864)
                self.assertIsNone(result['pnl']);self.assertFalse(result['promotion_allowed'])
                empty=result['panels'][f'120/up/own_field/state_level/date_price_bin/{DATE}/0:20']
                self.assertEqual(empty['planned'],0)
                rows[1]['signals']['up']['state_level']=False
                f.write_text(json.dumps(frozen));protocol['frozen_report_sha256']=bounds.file_hash(f)
                with self.assertRaisesRegex(ValueError,'signal'):
                    bounds.run(db,tmp,f,protocol)


if __name__=='__main__':unittest.main()
