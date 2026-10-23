import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from analysis import offline_price_calibration_audit as audit

spec = importlib.util.spec_from_file_location('price_calibration_synthetic', Path(__file__).with_name('test_offline_exit_continuation_audit.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def simple_candidates(predictions=(.8, .9, .85), decisions=None):
    decisions = decisions or [i * 100 for i in range(len(predictions))]
    rows = []
    start = pd.Timestamp('2026-01-01T00:00:00Z')
    for i, (prediction, second) in enumerate(zip(predictions, decisions)):
        slug = 'btc-updown-5m-' + str(int(start.timestamp()) + i * 1000)
        rows.append({'market_id': 'run::' + slug, 'run_name': 'run', 'slug': slug, 'side': 'up',
            'decision_ts': start + pd.Timedelta(seconds=second), 'market_close_ts': start + pd.Timedelta(seconds=second + 300),
            'decision_ask_price': .5, 'decision_sell_size': 1000., 'predicted_exit_bid_price': prediction})
    return pd.DataFrame(rows, columns=audit.prior.CANDIDATE_FIELDS)


def simple_labels(candidates, bids=(.6, .95, .5)):
    labels = []
    for i, (_, row) in enumerate(candidates.iterrows()):
        quote = row['decision_ts'] + pd.Timedelta(seconds=50)
        labels.append({'candidate_index': i, 'execution_index': i,
            **{k: row[k] for k in ('market_id', 'run_name', 'slug', 'side')},
            'decision_ts': row['decision_ts'].isoformat(), 'market_close_ts': row['market_close_ts'].isoformat(),
            'original_predicted_exit_bid_price': row['predicted_exit_bid_price'],
            'future_bid_price': bids[i], 'original_price_residual': row['predicted_exit_bid_price'] - bids[i],
            'target_status': 'known', 'label_available_ts': quote.isoformat(), 'target_exit_quote_ts': quote.isoformat()})
    return labels


class CalibrationMathTests(unittest.TestCase):
    def test_original_errors_not_recursively_calibrated(self):
        frame = simple_candidates()
        transformed, trace = audit.calibrate_candidates(frame, simple_labels(frame))
        self.assertAlmostEqual(trace[1]['delta'], .2)
        self.assertAlmostEqual(trace[1]['calibrated_predicted_exit_bid_price'], .7)
        self.assertAlmostEqual(trace[2]['delta'], .075)
        self.assertAlmostEqual(trace[2]['calibrated_predicted_exit_bid_price'], .775)
        self.assertAlmostEqual(trace[2]['used_reference_errors'][1], -.05)
        self.assertTrue(transformed.drop(columns='predicted_exit_bid_price').equals(frame.drop(columns='predicted_exit_bid_price')))
        self.assertEqual(list(transformed.index), list(frame.index))

    def test_current_and_future_label_perturbations_cannot_change_earlier_forecasts(self):
        frame = simple_candidates()
        labels = simple_labels(frame)
        _, original = audit.calibrate_candidates(frame, labels)
        for target in (1, 2):
            changed = copy.deepcopy(labels)
            changed[target]['future_bid_price'] = .01
            changed[target]['original_price_residual'] = changed[target]['original_predicted_exit_bid_price'] - .01
            _, trace = audit.calibrate_candidates(frame, changed)
            self.assertEqual(trace[:target + 1], original[:target + 1])

    def test_label_cutoff_strict_equality_and_nanoseconds(self):
        frame = simple_candidates()
        labels = simple_labels(frame)
        labels[0]['target_exit_quote_ts'] = labels[0]['label_available_ts'] = frame.iloc[1]['decision_ts'].isoformat()
        _, trace = audit.calibrate_candidates(frame, labels)
        self.assertEqual(trace[1]['reference_count'], 0)
        labels[0]['target_exit_quote_ts'] = labels[0]['label_available_ts'] = (frame.iloc[1]['decision_ts'] - pd.Timedelta(nanoseconds=1)).isoformat()
        _, trace = audit.calibrate_candidates(frame, labels)
        self.assertEqual(trace[1]['reference_count'], 1)
        self.assertLess(audit.prior._time(trace[1]['max_used_label_ts']), frame.iloc[1]['decision_ts'])

    def test_same_decision_never_feedback_and_stable_engine_order(self):
        frame = simple_candidates(decisions=[0, 0, 100])
        labels = simple_labels(frame)
        _, trace = audit.calibrate_candidates(frame, labels)
        self.assertEqual(trace[0]['reference_count'], 0)
        self.assertEqual(trace[1]['reference_count'], 0)
        self.assertEqual(trace[2]['reference_count'], 2)
        frame = frame.iloc[[2, 1, 0]].reset_index(drop=True)
        _, reordered = audit.calibrate_candidates(frame, list(reversed(labels)))
        self.assertEqual([r['market_id'] for r in reordered], [r['market_id'] for r in trace])
        self.assertEqual(reordered[0]['candidate_index'], 2)

    def test_unknown_excluded_not_zero_error(self):
        frame = simple_candidates()
        labels = simple_labels(frame)
        labels[0].update(target_status='exit_unknown_quote_conflict', future_bid_price=None,
                         original_price_residual=None, label_available_ts=None, target_exit_quote_ts=None)
        _, trace = audit.calibrate_candidates(frame, labels)
        self.assertEqual(trace[1]['reference_count'], 0)
        self.assertEqual(trace[2]['reference_count'], 1)
        self.assertEqual(trace[2]['delta'], 0.)
        self.assertEqual(trace[2]['calibrated_predicted_exit_bid_price'], .85)

    def test_delta_never_negative_or_raise_forecast_and_clips_zero(self):
        frame = simple_candidates((.8, .1, .1))
        labels = simple_labels(frame, (.01, .99, .99))
        _, trace = audit.calibrate_candidates(frame, labels)
        self.assertEqual(trace[1]['calibrated_predicted_exit_bid_price'], 0.)
        self.assertEqual(trace[2]['delta'], 0.)
        self.assertEqual(trace[2]['calibrated_predicted_exit_bid_price'], .1)
        self.assertTrue(all(0 <= r['calibrated_predicted_exit_bid_price'] <= r['original_predicted_exit_bid_price'] for r in trace))

    def test_duplicate_candidate_and_label_identity_rejected(self):
        frame = simple_candidates()
        labels = simple_labels(frame)
        for duplicate_candidate in (True, False):
            bad_frame, bad_labels = frame.copy(), copy.deepcopy(labels)
            if duplicate_candidate:
                bad_frame.loc[1, 'market_id'] = bad_frame.loc[0, 'market_id']
            else:
                bad_labels[1]['market_id'] = bad_labels[0]['market_id']
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                audit.calibrate_candidates(bad_frame, bad_labels)

    def test_invalid_known_and_zero_filled_unknown_rejected(self):
        frame = simple_candidates()
        labels = simple_labels(frame)
        for mutation in ('bool', 'nonfinite', 'wrong_original', 'unknown_zero', 'unknown_status'):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(labels)
                if mutation == 'bool': changed[0]['future_bid_price'] = True
                elif mutation == 'nonfinite': changed[0]['original_price_residual'] = float('nan')
                elif mutation == 'wrong_original': changed[0]['original_predicted_exit_bid_price'] = .7
                elif mutation == 'unknown_status': changed[0]['target_status'] = 'arbitrary'
                else: changed[0].update(target_status='exit_missing_book', future_bid_price=0., original_price_residual=0., label_available_ts=None)
                with self.assertRaises(ValueError): audit.calibrate_candidates(frame, changed)


class CalibrationAuditTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.ContinuationAuditTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.root = self.h.root
        self.refresh_config()

    def refresh_config(self):
        # Only synthetic rows/books are loaded and replayed in this test harness.
        self.h.reference = self.h.old_stopping_reference()
        self.h.config = self.h.outer_config()
        parent_result = self.h.run_continuation(self.root / ('parent' + str(len(list(self.root.glob('parent*'))))))
        self.config = {'evaluation_mode': 'offline_price_calibration',
            'parent_execution_config': copy.deepcopy(self.h.config),
            'parent_execution_config_sha256': audit.prior.digest(audit.prior.canonical(self.h.config)),
            'source_commit': self.h.config['source_commit'], 'source_repository': self.h.config['source_repository'],
            'candidate_binding': copy.deepcopy(self.h.binding), 'expected_code_commit': 'f' * 40,
            'code_file_sha256': {k: 'e' * 64 for k in audit.REQUIRED_CODE_FILES},
            'baseline_binding': {'expected_full_pooled_fingerprint': audit.prior.fingerprints.result_fingerprint(parent_result['full_ledger'])},
            'calibration_policy': dict(audit.POLICY), 'max_producer_replays': 2, 'max_calibrated_forecast_transforms': 50,
            'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0, 'live_orders_enabled': False}
        self.expected = parent_result

    def evaluate(self, name='out'):
        with patch.object(audit.prior, 'verified_code_source', return_value={'source_commit': 'a' * 40, 'code_commit': 'f' * 40}):
            return audit.evaluate(self.h.source, self.h.candidate_path, self.config, self.root / name)

    def read_jsonl(self, name, folder='out'):
        return [json.loads(line) for line in (self.root / folder / name).read_bytes().splitlines()]

    def test_two_fullstreams_single_raw_scan_and_exact_baseline(self):
        original_load = audit.prior.load_candidate_quote_books
        original_replay = audit.prior.execution.replay_cached_quotes
        with patch.object(audit.prior, 'load_candidate_quote_books', wraps=original_load) as loader, patch.object(audit.prior.execution, 'replay_cached_quotes', wraps=original_replay) as replay:
            result = self.evaluate()
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(replay.call_count, 2)
        self.assertTrue(all(call.kwargs['continue_after_required_fills'] for call in replay.call_args_list))
        self.assertEqual(result['arms'][audit.BASE_ARM], self.expected['full_ledger'])
        self.assertEqual(result['prefix_ledgers'][audit.BASE_ARM], self.expected['prefix_ledger'])
        self.assertTrue(result['baseline_control_reproduced'])
        self.assertTrue(result['prefix_control_reproduced'])
        self.assertTrue(result['comparison_valid'])
        self.assertEqual(result['producer_replay_completions'], 2)
        self.assertEqual(result['replayed_cells'], 2)
        self.assertEqual(result['calibration_forecast_transforms'], 12)
        self.assertEqual(result['reference_label_updates'], 12)
        self.assertEqual(result['actual_model_fit_calls'], 0)
        self.assertEqual(result['actual_model_predict_calls'], 0)
        self.assertEqual(set(p.name for p in (self.root / 'out').iterdir()), audit.OUTPUTS)
        self.assertEqual(set(result['arms']), set(audit.ARMS))
        for phase, requests in result['quote_requests'].items():
            self.assertEqual([r['request_index'] for r in requests], list(range(len(requests))))
            self.assertTrue(all(r['arm'] == phase for r in requests))
        attempts = self.read_jsonl('exit-attempts.jsonl')
        self.assertEqual(set(row['arm'] for row in attempts), set(audit.ARMS))
        witness = self.read_jsonl('quote-witness.jsonl')
        self.assertEqual(set(row['arm'] for row in witness), set(audit.PHASES))
        trace = self.read_jsonl('calibration-trace.jsonl')
        self.assertEqual(len(trace), 12)
        self.assertTrue(all(row['max_used_label_ts'] is None or audit.prior._time(row['max_used_label_ts']) < audit.prior._time(row['decision_ts']) for row in trace))

    def test_baseline_full_failure_stops_before_reference_and_treatment(self):
        self.config['baseline_binding']['expected_full_pooled_fingerprint'] = '0' * 64
        original = audit.prior.execution.replay_cached_quotes
        with patch.object(audit.prior.execution, 'replay_cached_quotes', wraps=original) as replay:
            with self.assertRaisesRegex(ValueError, 'evaluation_failed'): self.evaluate()
        self.assertEqual(replay.call_count, 1)
        result = json.loads((self.root / 'out/summary.json').read_text())
        self.assertFalse(result['comparison_valid'])
        self.assertEqual(result['producer_replay_completions'], 1)
        self.assertEqual(result['calibration_forecast_transforms'], 0)
        self.assertEqual(result['quote_requests'][audit.REFERENCE_ARM], [])
        self.assertEqual(result['quote_requests'][audit.TREATMENT_ARM], [])
        self.assertFalse((self.root / 'out/treatment-ledger.json').exists())

    def test_baseline_prefix_guard_fails_before_tail_or_calibration(self):
        frozen = self.config['parent_execution_config']['prefix_binding']
        frozen['frozen_pooled_result']['chain'][0]['entry_fee'] += .01
        raw = audit.prior.canonical(frozen['frozen_pooled_result']) + b'\n'
        frozen.update(expected_pooled_fingerprint=audit.prior.fingerprints.result_fingerprint(frozen['frozen_pooled_result']),
                      parent_retry_ledger_bytes=len(raw), parent_retry_ledger_sha256=audit.prior.digest(raw))
        self.config['parent_execution_config_sha256'] = audit.prior.digest(audit.prior.canonical(self.config['parent_execution_config']))
        with self.assertRaisesRegex(ValueError, 'evaluation_failed'): self.evaluate()
        result = json.loads((self.root / 'out/summary.json').read_text())
        self.assertFalse(result['prefix_control_reproduced'])
        self.assertEqual(result['producer_replay_completions'], 0)
        self.assertEqual(len(result['quote_requests'][audit.BASE_ARM]), 20)
        self.assertEqual(result['quote_requests'][audit.REFERENCE_ARM], [])
        self.assertEqual(result['quote_requests'][audit.TREATMENT_ARM], [])

    def test_labels_and_all_transforms_continue_beyond_saved_baseline_hard_stop(self):
        self.h.add_market(12 * 1300)
        losing_slug = self.h.rows[10]['slug']
        target = audit.prior._time(self.h.rows[10]['decision_ts']) + pd.Timedelta(seconds=50)
        for row in self.h.raw:
            if row['slug'] == losing_slug and audit.prior._time(row['ts_iso']) == target:
                row['sell_up_cents'] = row['sell_down_cents'] = 5.
        self.h.write_inputs()
        self.h.parent.update(candidate_binding=self.h.binding, frozen_control_candidates=self.h.rows,
            expected_control_candidate_count=len(self.h.rows),
            frozen_control_candidate_rows_sha256=audit.prior.digest(audit.prior.canonical(self.h.rows)))
        self.refresh_config()
        result = self.evaluate()
        baseline = result['arms'][audit.BASE_ARM]
        self.assertEqual(baseline['continuation_failure_reason'], 'cash_cannot_fit_minimum_order')
        self.assertLess(baseline['processed_candidate_count'], len(self.h.rows))
        self.assertEqual(result['reference_label_updates'], len(self.h.rows))
        self.assertEqual(result['calibration_forecast_transforms'], len(self.h.rows))
        labels = self.read_jsonl('reference-labels.jsonl')
        self.assertEqual(labels[-1]['market_id'], self.h.rows[-1]['market_id'])
        self.assertEqual(labels[-1]['target_status'], 'known')

    def test_parent_hash_and_runtime_fail_before_candidate_or_raw(self):
        for mutation in ('parent', 'runtime', 'budget', 'variant'):
            with self.subTest(mutation=mutation):
                config = copy.deepcopy(self.config)
                if mutation == 'parent': config['parent_execution_config_sha256'] = '0' * 64
                elif mutation == 'budget': config['max_calibrated_forecast_transforms'] = True
                elif mutation == 'variant': config['calibration_policy']['method'] = 'another'
                if mutation == 'runtime': self.h.probe.return_value = {**fixture.fixtures.runtime_fixture(), 'python_major_minor': '0.0'}
                try:
                    with patch.object(audit.prior, 'load_frozen_candidates') as candidates, patch.object(audit.prior, 'load_candidate_quote_books') as raw:
                        with self.assertRaisesRegex(ValueError, 'evaluation_failed'):
                            audit.evaluate(self.h.source, self.h.candidate_path, config, self.root / mutation)
                        candidates.assert_not_called()
                        raw.assert_not_called()
                finally:
                    self.h.probe.return_value = fixture.fixtures.runtime_fixture()

    def test_unknown_reference_conflict_and_invalid_not_zero(self):
        first = self.h.rows[0]['slug']
        self.h.raw.append(fixture.fixtures.quote(first, 170, bid=61., size=1e9))
        self.h.write_inputs()
        source, _, candidates = self.h.source_and_candidates()
        source.phase = audit.REFERENCE_ARM
        labels = audit.extract_reference_labels(candidates, source, self.h.base)
        self.assertEqual(labels[0]['target_status'], 'exit_unknown_quote_conflict')
        self.assertIsNone(labels[0]['original_price_residual'])
        _, trace = audit.calibrate_candidates(candidates, labels)
        self.assertEqual(trace[1]['reference_count'], 0)

    def test_reference_label_ignores_depth_and_ordinary_invalid_price_uses_original_window(self):
        first = self.h.rows[0]['slug']
        for row in self.h.raw:
            if row['slug'] == first and row['ts_iso'] == fixture.fixtures.quote(first, 170)['ts_iso']:
                row['sell_up_cents'] = 'invalid'
                row['sell_up_size'] = 0.
        self.h.raw.append(fixture.fixtures.quote(first, 171, bid=65., size=0.))
        self.h.write_inputs()
        source, _, candidates = self.h.source_and_candidates()
        source.phase = audit.REFERENCE_ARM
        labels = audit.extract_reference_labels(candidates, source, self.h.base)
        self.assertEqual(labels[0]['target_status'], 'known')
        self.assertEqual(labels[0]['future_bid_price'], .65)
        self.assertEqual(labels[0]['target_exit_arrival_ts'], fixture.fixtures.quote(first, 170)['ts_iso'])
        self.assertEqual(labels[0]['target_exit_quote_ts'], fixture.fixtures.quote(first, 171)['ts_iso'])
        self.assertEqual(labels[0]['label_available_ts'], labels[0]['target_exit_quote_ts'])
        self.assertEqual(len(source.requests), 24)

    def test_original_entry_selected_time_drives_exit_not_fixed_decision_offset(self):
        first = self.h.rows[0]['slug']
        self.h.raw = [r for r in self.h.raw if r['slug'] != first]
        self.h.raw.extend([fixture.fixtures.quote(first, 134), fixture.fixtures.quote(first, 170, bid=20.), fixture.fixtures.quote(first, 172, bid=66.)])
        self.h.write_inputs()
        source, _, candidates = self.h.source_and_candidates()
        source.phase = audit.REFERENCE_ARM
        label = audit.extract_reference_labels(candidates, source, self.h.base)[0]
        self.assertEqual(label['target_entry_quote_ts'], fixture.fixtures.quote(first, 134)['ts_iso'])
        self.assertEqual(label['target_exit_arrival_ts'], fixture.fixtures.quote(first, 172)['ts_iso'])
        self.assertEqual(label['future_bid_price'], .66)

    def test_treatment_fewer_than_ten_is_valid_with_null_prefix_and_not_economic_pass(self):
        # Identify original exits using exact frozen clocks rather than minute fields.
        for i, candidate in enumerate(self.h.rows):
            original_clock = audit.prior._time(candidate['decision_ts']) + pd.Timedelta(seconds=50)
            for row in self.h.raw:
                if row['slug'] == candidate['slug'] and audit.prior._time(row['ts_iso']) == original_clock:
                    row['sell_up_cents'] = row['sell_down_cents'] = 5.
                    row['sell_up_size'] = row['sell_down_size'] = 0.
            self.h.raw.append(fixture.fixtures.quote(candidate['slug'], i * 1300 + 182, bid=60., size=1e9))
        self.h.write_inputs()
        self.refresh_config()
        result = self.evaluate()
        treatment = result['arms'][audit.TREATMENT_ARM]
        self.assertTrue(result['comparison_valid'])
        self.assertLess(treatment['resolved_fills'], 10)
        self.assertIsNone(result['prefix_ledgers'][audit.TREATMENT_ARM])
        self.assertIsNone(json.loads((self.root / 'out/treatment-prefix-ledger.json').read_text()))
        self.assertFalse(result['arm_endpoints'][audit.TREATMENT_ARM]['full_economic_pass'])
        self.assertEqual(result['calibration_forecast_transforms'], len(self.h.rows))
        self.assertEqual(result['reference_label_updates'], len(self.h.rows))

    def test_nontraded_candidate_reference_still_used_and_all_transforms_saved(self):
        self.h.rows[1]['predicted_exit_bid_price'] = '.51'
        self.h.write_inputs()
        self.h.parent.update(candidate_binding=self.h.binding, frozen_control_candidates=self.h.rows,
            frozen_control_candidate_rows_sha256=audit.prior.digest(audit.prior.canonical(self.h.rows)))
        self.refresh_config()
        result = self.evaluate()
        rejected = next(r for r in result['arms'][audit.BASE_ARM]['attempts'] if r['market_id'] == self.h.rows[1]['market_id'])
        self.assertEqual(rejected['status'], 'decision_cost_floor_not_met')
        trace = self.read_jsonl('calibration-trace.jsonl')
        self.assertIn(self.h.rows[1]['market_id'], trace[2]['used_market_ids'])
        self.assertEqual(len(trace), len(self.h.rows))

    def test_full_primary_requires_growth_above_initial_and_required_fills(self):
        base = self.h.base
        for resolved, cash, expected in ((9, 12., False), (10, 10., False), (10, 10. + 5e-10, True), (10, 9., False)):
            full = {'full_sequence_resolved': True, 'resolved_fills': resolved, 'ending_cash': cash,
                    'full_sequence_net_growth': False}
            with patch.object(audit, 'optional_prefix_readouts', return_value={}):
                self.assertIs(audit.arm_endpoints(full, None, base)['full_economic_pass'], expected)

    def test_evidence_overflow_preserves_original_failure_summary(self):
        with patch.dict(audit.LIMITS, {'quote-witness.jsonl': 1}):
            with self.assertRaisesRegex(ValueError, 'evaluation_failed'):
                self.evaluate()
        saved = json.loads((self.root / 'out/summary.json').read_text())
        self.assertEqual(saved['failure']['stage'], 'baseline_fullstream')
        self.assertEqual(saved['failure']['code'], 'combined_quote_witness_budget')
        self.assertEqual(saved['producer_replayed_cells'], 1)
        self.assertEqual(saved['producer_replay_completions'], 0)
        self.assertFalse(saved['comparison_valid'])
        self.assertFalse(saved['strategy_evaluated'])
        self.assertEqual(saved['artifact_failures'][0]['member'], 'quote-witness.jsonl')
        self.assertFalse((self.root / 'out/quote-witness.jsonl').exists())
        self.assertTrue((self.root / 'out/exit-attempts.jsonl').exists())

    def test_combined_witness_budget_and_phase_isolation(self):
        source, _, _ = self.h.source_and_candidates()
        peers = []
        one = audit.PhaseQuoteSource(source, audit.BASE_ARM, peers)
        two = audit.PhaseQuoteSource(source, audit.REFERENCE_ARM, peers)
        candidate = self.h.rows[0]
        kwargs = {'side': 'up', 'kind': 'entry', 'max_quote_delay_seconds': 3.}
        target = audit.prior._time(candidate['decision_ts']) + pd.Timedelta(seconds=12)
        one.select(candidate['market_id'], target, audit.prior._time(candidate['market_close_ts']), **kwargs)
        self.assertEqual(two.requests, [])
        self.assertEqual(one.requests[0]['request_index'], 0)
        with patch.dict(audit.LIMITS, {'quote-witness.jsonl': one.witness_bytes + 1}):
            with self.assertRaisesRegex(ValueError, 'combined_quote_witness_budget'):
                two.select(candidate['market_id'], target, audit.prior._time(candidate['market_close_ts']), **kwargs)


if __name__ == '__main__':
    unittest.main()
