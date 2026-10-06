import copy
import csv
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from analysis import quote_measurement_audit as audit
from analysis import archive_coverage_ledger as ledger
from analysis import price_clock_diagnostic as state
from tests import test_price_clock_diagnostic as fixture
from tests.test_archive_coverage_ledger import DATE, START
from tests.test_historical_walk_forward import config, quote


def rows():
    return fixture.quotes()


def inspect(raw=None, offset=120, **kwargs):
    return audit.inspect_slot(rows() if raw is None else raw, START, offset, config(), **kwargs)


def changed(raw, at, index, value):
    for r in raw:
        if r['ts'] == START+at:
            v = list(r['values']); v[index] = value; r['values'] = tuple(v)
            r['price_reason'] = ledger.price_reason(v[:4]); r['depth_reason'] = ledger.depth_reason(v[4:])
    return raw


class MeasurementAuditTests(unittest.TestCase):
    def test_offsets_are_not_horizons_and_targets_stay_in_window(self):
        for o in (60,120,240):
            r = inspect(offset=o)
            self.assertEqual(r['target_us'], (START+o+30)*1000000)
            self.assertFalse(r['crosses_window_end'])

    def test_current_strict_before_lag_inclusive_and_entry_strict_after(self):
        raw = rows()+[quote(START,105),quote(START,120)]
        raw.sort(key=lambda r:r['ts']); r = inspect(raw)
        self.assertEqual([r['roles'][x]['ts']-START for x in ('current','lag','entry','exit')],[119,105,121,150])

    def test_no_future_record_and_late_record_are_distinct(self):
        raw = [r for r in rows() if r['ts']<START+150]
        self.assertEqual(inspect(raw)['timing'],'no_record_at_or_after_target')
        raw.append(quote(START,154))
        self.assertEqual(inspect(raw)['timing'],'first_record_after_tolerance')

    def test_tolerance_boundary_inclusive(self):
        raw = [r for r in rows() if r['ts']!=START+150]+[quote(START,153)]
        raw.sort(key=lambda r:r['ts'])
        self.assertEqual(inspect(raw)['timing'],'record_within_tolerance')

    def test_first_bad_exit_never_skipped_to_valid_later_quote(self):
        raw = changed(rows(),150,1,None)+[quote(START,151)]
        raw.sort(key=lambda r:r['ts']); s = inspect(raw)['side']['up']
        self.assertEqual(s['exit_cause_disjoint'],'own_bid_missing_or_invalid')
        self.assertTrue(s['exit_own_bid_later_recovers'])
        self.assertFalse(s['paths']['1']['price_only_known'])

    def test_missing_opposite_ask_is_common_label_policy_not_missing_own_bid(self):
        s = inspect(changed(rows(),150,2,None))['side']['up']
        self.assertEqual(s['exit_cause_disjoint'],'opposite_book_invalid')
        self.assertTrue(s['exit_bid_field_valid_in_time'])
        self.assertTrue(s['paths']['1']['side_only_price_available'])
        self.assertFalse(s['paths']['1']['price_only_known'])

    def test_own_missing_ask_separate_from_missing_bid(self):
        s = inspect(changed(rows(),150,0,None))['side']['up']
        self.assertEqual(s['exit_cause_disjoint'],'own_ask_invalid_or_side_crossed')
        self.assertTrue(s['exit_bid_field_valid_in_time'])

    def test_zero_size_is_not_absent_price(self):
        s = inspect(changed(rows(),150,5,0))['side']['up']
        self.assertTrue(s['paths']['1']['price_only_known'])
        self.assertFalse(s['paths']['1']['positive_depth_known'])
        self.assertFalse(s['exit_bid_positive_depth_in_time'])

    def test_invalid_first_entry_not_skipped_and_late_entry_unknown(self):
        s = inspect(changed(rows(),121,0,None))['side']['up']
        self.assertFalse(s['paths']['1']['price_only_known'])
        raw = [r for r in rows() if r['ts']!=START+121]
        self.assertFalse(inspect(raw)['side']['up']['paths']['1']['entry_before_exit'])

    def test_crossed_book_and_boundary_prices_are_distinct_from_missing(self):
        self.assertEqual(audit.book_reason([40,42,60,59]),'crossed_price')
        self.assertEqual(audit.book_reason([0,0,60,59]),'price_outside_binary_range')

    def test_absent_invalid_time_and_feature_missing_keep_denominators(self):
        self.assertEqual(inspect([],present=False)['status'],'absent_window')
        self.assertEqual(inspect([],present=True)['status'],'present_invalid_time_only')
        self.assertEqual(inspect([quote(START,121)])['status'],'excluded_decision_missing_or_stale')
        c = audit.Counter(); r = {'eligible':False}
        audit.update_counts(c,r,inspect([],present=False),'up',False)
        self.assertEqual((c['planned'],c['feature_valid'],c['selected']),(1,0,0))

    def test_duplicate_and_cross_window_fail(self):
        for raw in (rows()+[rows()[0]],rows()+[quote(START,300)]):
            with self.assertRaisesRegex(ValueError,'duplicate_or_cross_window'):
                inspect(sorted(raw,key=lambda r:r['ts']))

    def test_independent_match_agrees_and_rejects_changed_labels_signals(self):
        r = fixture.sample(); fits = fixture.fits(); state.populate(r,fits,fixture.legacy(),1)
        d = inspect(); audit.check_frozen(r,d,config(),fits)
        for key in ('label','signal','time','prediction'):
            bad = copy.deepcopy(r)
            if key=='label':bad['side_targets']['up']['bid_change_cents'] += 1
            if key=='signal':bad['signals']['up']['state_level'] = not bad['signals']['up']['state_level']
            if key=='time':bad['observed_exit_time'] += 1
            if key=='prediction':bad['predictions']['up']['bid_change_cents']['state_level'] += 1
            with self.assertRaisesRegex(ValueError,'frozen_measurement_mismatch'):
                audit.check_frozen(bad,d,config(),fits)

    def test_full_book_policy_agrees_with_frozen_labels(self):
        raw = changed(rows(),150,2,None)
        r = fixture.sample(rows=raw); fits = fixture.fits(); state.populate(r,fits,fixture.legacy(),1)
        audit.check_frozen(r,inspect(raw),config(),fits)

    def test_bins_keep_unavailable_and_fixed_boundaries(self):
        self.assertEqual([audit.price_bin(v) for v in (None,0,1,20,99,100)],['unavailable','unavailable','0:20','20:40','80:100','unavailable'])

    def test_raw_empty_zero_and_malformed_distinguished(self):
        self.assertEqual([audit.raw_field_class(x) for x in ('','0','nan','-1','2')],['empty','zero','malformed_or_nonfinite','negative','positive'])

    def test_raw_scan_checks_cells_source_lines_and_file_bounds(self):
        row = {'ts_iso':DATE+'T00:02:30Z','slug':f'btc-updown-5m-{START}'}
        for f in audit.PRICE_FIELDS:row[f]='45'
        for f in audit.SIZE_FIELDS:row[f]='2'
        row['level_count_bid_up']='0'
        stream=io.StringIO(); writer=csv.DictWriter(stream,fieldnames=list(row));writer.writeheader();writer.writerow(row)
        raw=stream.getvalue().encode(); ts=ledger.utc_us(row['ts_iso']); key=(row['slug'],ts)
        info={'path':'sample.csv','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'sample.csv').write_bytes(raw)
            summary, points, windows = audit.scan_raw(tmp,[info],{key:[45]*4+[2]*4})
            self.assertEqual(summary['counts']['rows'],1)
            self.assertEqual(points[key]['sources'][0]['csv_line'],2)
            self.assertEqual(points[key]['cells']['level_count_bid_up'],'0')
            self.assertEqual(windows[row['slug']][0]['file_last_us'],ts)
            with self.assertRaisesRegex(ValueError,'values_mismatch'):
                audit.scan_raw(tmp,[info],{key:[46]*4+[2]*4})
            with self.assertRaisesRegex(ValueError,'records_missing'):
                audit.scan_raw(tmp,[info],{(row['slug'],ts+1):[45]*4+[2]*4})


if __name__ == '__main__':
    unittest.main()
