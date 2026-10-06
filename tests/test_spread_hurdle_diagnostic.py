import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from analysis import archive_coverage_ledger as ledger
from analysis import historical_walk_forward as walk
from analysis import spread_hurdle_diagnostic as hurdle
from tests.test_archive_coverage_ledger import DATE, START, observation, write_csv
from tests.test_historical_walk_forward import config, quote


ZERO = {'id': 'zero', 'fee_cents_per_share_per_leg': 0, 'slippage_cents_per_share_per_leg': 0,
        'historical_actual_cost': False}
COST = dict(ZERO, id='assumed', fee_cents_per_share_per_leg=0.5, slippage_cents_per_share_per_leg=0.5)


def plan():
    return {'signal_rule': 'strict_positive_predicted_spread_headroom',
            'down_score': 'negative_frozen_up_ask_change_directional_proxy',
            'selection': 'none_all_candidates_and_scenarios', 'gate_cost_scenario': 'zero',
            'budget_cents': 1000, 'cost_scenarios': [ZERO, COST]}


def path_row(*, future=46, prediction=4, down_prediction=-4, eligible=True, ask_size=10):
    return {'market': 'm', 'date': DATE, 'fold': 0, 'start': START, 'eligible': eligible,
            'status': 'absent_window' if not eligible else 'unknown_future_missing' if future is None else 'labeled_quote_change',
            'predictions': {'bid_change_cents': {'zero': 0, 'candidate': prediction},
                            'ask_change_cents': {'zero': 0, 'candidate': down_prediction}},
            'sides': {s: {'ask_cents': 43, 'bid_cents': 40, 'spread_cents': 3,
                         'reported_ask_size': ask_size, 'future_bid_cents': future,
                         'observed_spread_headroom_cents': None if future is None else future - 43}
                      for s in ('up', 'down')}}


def write_observations(root, grouped):
    rows = []
    for slug, raw in grouped.items():
        for x in raw:
            values = dict(zip(ledger.PRICE_FIELDS + ledger.SIZE_FIELDS, x['values']))
            rows.append(observation(slug=slug, ts_iso=datetime.fromtimestamp(x['ts'], timezone.utc).isoformat(), **values))
    write_csv(root, 'quotes.csv', rows)


class SpreadHurdleTests(unittest.TestCase):
    def test_improved_bid_below_spread_is_not_positive_headroom(self):
        r = path_row(future=41, prediction=1)
        self.assertGreater(r['sides']['up']['future_bid_cents'], r['sides']['up']['bid_cents'])
        self.assertEqual(r['sides']['up']['observed_spread_headroom_cents'], -2)
        self.assertFalse(hurdle.signal(r, 'candidate', 'up'))
        r['predictions']['bid_change_cents']['candidate'] = 3
        self.assertFalse(hurdle.signal(r, 'candidate', 'up'))

    def test_down_direction_score_never_synthesizes_down_price(self):
        r = path_row()
        r['sides']['down'].update(ask_cents=71, bid_cents=67, spread_cents=4,
                                future_bid_cents=80, observed_spread_headroom_cents=9)
        r['predictions']['ask_change_cents']['candidate'] = -5
        self.assertTrue(hurdle.signal(r, 'candidate', 'down'))
        score = hurdle.aggregate([r], ['candidate'], 'down', ZERO, 1000)
        self.assertEqual(score['policies']['candidate']['selected_observed_mean_quote_margin_cents'], 9)

    def test_unknown_bid_keeps_denominator_and_exact_worst_case_bounds(self):
        r = path_row(future=None)
        a = hurdle.aggregate([r], ['candidate'], 'up', ZERO, 1000)
        self.assertEqual((a['scheduled_windows'], a['eligible_windows'], a['shared_labeled_windows'], a['unknown_future_quotes']), (1, 1, 0, 1))
        m = a['policies']['candidate']
        self.assertEqual((m['selected_eligible_lower_cents'], m['selected_eligible_upper_cents']), (-43, 57))
        self.assertIsNone(m['selected_observed_mean_quote_margin_cents'])

    def test_same_labeled_comparison_not_candidate_selected_denominator(self):
        rows = [path_row(future=46), path_row(future=40, prediction=0), path_row(future=None)]
        a = hurdle.aggregate(rows, ['zero', 'candidate'], 'up', ZERO, 1000)
        self.assertEqual(a['shared_labeled_windows'], 2)
        c = a['policies']['candidate']
        self.assertEqual(c['selected_observed_windows'], 1)
        self.assertEqual(c['selected_observed_mean_quote_margin_cents'], 3)
        self.assertEqual(c['shared_label_activity_weighted_mean_cents'], 1.5)
        self.assertEqual(a['policies']['always_same_side']['shared_label_activity_weighted_mean_cents'], 0)
        self.assertEqual(c['paired_vs']['always_same_side']['same_label_mean_cents'], 1.5)

    def test_costs_do_not_reselect_signals_and_charge_both_legs(self):
        rows = [path_row(), path_row(future=None), path_row(prediction=0)]
        z = hurdle.aggregate(rows, ['candidate'], 'up', ZERO, 1000)['policies']['candidate']
        c = hurdle.aggregate(rows, ['candidate'], 'up', COST, 1000)['policies']['candidate']
        self.assertEqual(z['selected_windows'], c['selected_windows'])
        self.assertEqual(z['selected_observed_mean_quote_margin_cents'] - c['selected_observed_mean_quote_margin_cents'], 2)
        self.assertEqual(z['selected_eligible_lower_cents'] - c['selected_eligible_lower_cents'], 2)

    def test_absent_signal_windows_have_separate_calendar_stress(self):
        rows = [path_row(), path_row(eligible=False)]
        a = hurdle.aggregate(rows, ['candidate'], 'up', ZERO, 1000)
        self.assertEqual(a['unobservable_signal_windows'], 1)
        self.assertEqual(a['policies']['candidate']['full_calendar_missing_exposure_lower_cents'], -48.5)
        self.assertEqual(a['policies']['no_signal']['full_calendar_missing_exposure_lower_cents'], 0)

    def test_budget_example_never_invents_minimum_order_or_depth(self):
        book = path_row(ask_size=0.25)['sides']['up']
        b = hurdle.budget_example(book, 1000, 0)
        self.assertTrue(b['one_share_quote_fits_budget'])
        self.assertFalse(b['one_share_reported_depth_sufficient'])
        self.assertEqual(b['displayed_whole_share_cap'], 0)
        self.assertIsNone(b['order_feasibility'])
        self.assertFalse(b['minimum_order_verified'])
        book['reported_ask_size'] = None
        self.assertIsNone(hurdle.budget_example(book, 1000, 0)['displayed_whole_share_cap'])

    def test_unverified_actual_fees_nan_and_selection_are_rejected(self):
        for mutate in (lambda p: p['cost_scenarios'][1].update(historical_actual_cost=True),
                       lambda p: p['cost_scenarios'][1].update(fee_cents_per_share_per_leg=float('nan')),
                       lambda p: p.update(gate_cost_scenario='assumed'),
                       lambda p: p.update(selection='best_validation')):
            p = copy.deepcopy(plan()); mutate(p)
            with self.assertRaises(ValueError):
                hurdle.validate_plan(p)

    def fixture(self, root, *, bad_future=False, no_future=False):
        cfg = config()
        raw = [quote(START, 104, 49), quote(START, 119, 50), quote(START, 150, 52)]
        if no_future:
            raw.pop()
        if bad_future:
            raw[-1]['values'] = (None,) + raw[-1]['values'][1:]
            raw[-1]['price_reason'] = 'missing_or_nonfinite_price'
            raw.append(quote(START, 151, 70))
        market = f'btc-updown-5m-{START}'
        write_observations(root, {market: raw})
        dbpath = root / 'coverage.sqlite'; ledger.build(root, dbpath)
        sample = walk.sample_window(raw, market, START, DATE, cfg, 'common', [0, 100], True)
        sample.update(fold=0, predictions={'bid_change_cents': {'candidate': 3}, 'ask_change_cents': {'candidate': 3}})
        return dbpath, sample, cfg

    def test_exact_quote_join_uses_real_down_and_fails_on_feature_label_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            dbpath, row, cfg = self.fixture(Path(d))
            with ledger.connect_readonly(dbpath) as db:
                result = hurdle.attach_path(db, row, cfg)
                self.assertEqual(result['sides']['down']['observed_spread_headroom_cents'], -1)
                self.assertEqual(result['sides']['up']['observed_spread_headroom_cents'], 1)
                bad = copy.deepcopy(row); bad['features']['spread'] = 0
                with self.assertRaisesRegex(ValueError, 'feature_mismatch'):
                    hurdle.attach_path(db, bad, cfg)
                bad = copy.deepcopy(row); bad['targets']['bid_change_cents'] += 1
                with self.assertRaisesRegex(ValueError, 'target_mismatch'):
                    hurdle.attach_path(db, bad, cfg)

    def test_invalid_first_future_not_skipped_or_silently_removed(self):
        with tempfile.TemporaryDirectory() as d:
            dbpath, row, cfg = self.fixture(Path(d), bad_future=True)
            with ledger.connect_readonly(dbpath) as db:
                result = hurdle.attach_path(db, row, cfg)
            self.assertTrue(result['eligible'])
            self.assertEqual(result['future_status'], 'unknown_future_price')
            self.assertIsNone(result['sides']['down']['future_bid_cents'])

    def test_future_from_another_market_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as d:
            dbpath, row, cfg = self.fixture(Path(d), no_future=True)
            with sqlite3.connect(dbpath) as db:
                # Deliberately corrupt a synthetic ledger to expose any join
                # that looks up time without checking the market key.
                db.execute('INSERT INTO observations (slug,ts_us,start,prices,sizes) VALUES (?,?,?,?,?)',
                    ('different-market', (START + 150) * 1000000, START, '[91,90,31,30]', '[10,10,10,10]'))
            with ledger.connect_readonly(dbpath) as db:
                result = hurdle.attach_path(db, row, cfg)
            self.assertEqual(result['future_status'], 'unknown_future_missing')
            self.assertIsNone(result['sides']['up']['future_bid_cents'])

    def test_cross_window_source_and_zero_cost_failures_are_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            dbpath, row, cfg = self.fixture(Path(d))
            with sqlite3.connect(dbpath) as db:
                db.execute('UPDATE observations SET start=? WHERE ts_us=?',
                           (START + 300, (START + 150) * 1000000))
            with ledger.connect_readonly(dbpath) as db:
                with self.assertRaisesRegex(ValueError, 'cross_window_future'):
                    hurdle.attach_path(db, row, cfg)
        score = hurdle.aggregate([path_row(future=40)], ['candidate'], 'up', ZERO, 1000)
        self.assertEqual(score['policies']['candidate']['diagnostic_status'], 'failed_even_zero_cost_quote_hurdle')

    def test_actual_cli_reuses_fixed_predictions_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); cfg = config(); grouped = {}
            for day in (0, 1):
                for i in range(3):
                    start = START + day * 86400 + i * 300
                    grouped[f'btc-updown-5m-{start}'] = [quote(start, 104, 30 + i),
                        quote(start, 119, 31 + 2 * i), quote(start, 150, 32 + 3 * i)]
            write_observations(root, grouped)
            database = root / 'coverage.sqlite'; ledger.build(root, database)
            folds = [{'train_dates': [DATE], 'validation_dates': ['2026-09-27']}]
            with ledger.connect_readonly(database) as db:
                samples = walk.samples_for_dates(db, [DATE, '2026-09-27'], cfg, [0, 100])
            cohorts = {c: walk.evaluate(rows, folds, cfg, c) for c, rows in samples.items()}
            previous = {'plan': {'diagnostic': cfg, 'database_sha256': hurdle.file_hash(database)},
                        'protocol_sha256': 'synthetic-prior', 'cohorts': cohorts}
            source = root / 'previous.json'; source.write_text(json.dumps(previous))
            p = plan(); p.update(database_sha256=hurdle.file_hash(database), prediction_file_sha256=hurdle.file_hash(source),
                prior_protocol_sha256='synthetic-prior', code_sha256={},
                cohort_models={c:v['models'] for c,v in cohorts.items()},
                cohort_scheduled_windows={c:len(v['window_ledger']) for c,v in cohorts.items()})
            protocol = root / 'plan.json'; protocol.write_text(json.dumps(p)); output = root / 'output.json'
            process = subprocess.run([sys.executable, '-m', 'analysis.spread_hurdle_diagnostic',
                '--database', str(database), '--poly-root', str(root), '--predictions', str(source),
                '--protocol', str(protocol), '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn('PRIVATE_SPREAD_HURDLE_COMPLETE', process.stdout)
            result = json.loads(output.read_text())
            self.assertFalse(result['models_refit']); self.assertIsNone(result['pnl'])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            for c in cohorts:
                self.assertEqual(result['cohorts'][c]['sides']['up']['zero']['overall']['shared_labeled_windows'], 3)
            with patch('analysis.quote_factor_diagnostic.fit', side_effect=AssertionError('no refit')):
                hurdle.run(database, root, source, p)
            previous['cohorts']['common']['window_ledger'][0]['predictions']['bid_change_cents']['zero'] = 9
            source.write_text(json.dumps(previous)); p['prediction_file_sha256'] = hurdle.file_hash(source)
            with self.assertRaisesRegex(ValueError, 'prediction_changed'):
                hurdle.run(database, root, source, p)


if __name__ == '__main__':
    unittest.main()
