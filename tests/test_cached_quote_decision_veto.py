"""Opt-in decision veto contracts on tiny invented books only."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import MappingProxyType
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from analysis import cached_quote_execution as engine

spec = importlib.util.spec_from_file_location('decision_veto_tiny_fixture', Path(__file__).with_name('test_cached_quote_continuation.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def available(margin=.1):
    return {'prediction_status': 'available', 'predicted_net_margin_per_share': margin, 'reason': 'prediction_available'}


def unavailable(status='not_fitted'):
    return {'prediction_status': status, 'predicted_net_margin_per_share': None,
            'reason': engine.DECISION_VETO_REASONS[status]}


def play(rows, *, veto=None, books=None, snapshots=None, **kwargs):
    source = fixtures.Source(fixtures.fixture(rows) if books is None else books)
    result = engine.replay_cached_quotes(pd.DataFrame(rows), ['toy'], source,
        **fixtures.PARAMETERS, decision_veto=veto, continue_after_required_fills=True,
        required_prefix_observer=(lambda snapshot: snapshots.append(snapshot)) if snapshots is not None else lambda _: None,
        **kwargs)
    return result, source


class DecisionVetoTests(unittest.TestCase):
    def test_default_none_entire_mapping_matches_old_baseline_and_prefix(self):
        rows = [fixtures.row(i) for i in range(12)]
        books = fixtures.fixture(rows)
        old_source = fixtures.Source(books)
        prefix = []
        old = engine.replay_cached_quotes(pd.DataFrame(rows), ['toy'], old_source, **fixtures.PARAMETERS,
            continue_after_required_fills=True, required_prefix_observer=prefix.append)
        current_prefix = []
        actual, source = play(rows, books=books, snapshots=current_prefix)
        self.assertEqual(actual, old)
        self.assertEqual(source.calls, old_source.calls)
        self.assertEqual(current_prefix, prefix)
        self.assertNotIn('decision_veto_evidence', actual['chain'][0])
        expected = fixtures.reference.replay_timed_quotes(pd.DataFrame(rows), ['toy'], books, **fixtures.PARAMETERS)
        self.assertEqual(prefix, [expected])

    def test_all_candidates_original_order_processed_not_prefiltered(self):
        rows = [fixtures.row(2), fixtures.row(0), fixtures.row(1)]
        seen = []
        def callback(row):
            seen.append(row['market_id'])
            return available(-.1)
        result, source = play(rows, veto=callback)
        ordered = [fixtures.row(i)['market_id'] for i in range(3)]
        self.assertEqual(seen, ordered)
        self.assertEqual([a['market_id'] for a in result['attempts']], ordered)
        self.assertEqual(result['processed_candidate_count'], 3)
        self.assertEqual(result['candidate_stream_count'], 3)
        self.assertTrue(result['candidate_stream_exhausted'])
        self.assertEqual(source.calls, [])
        self.assertEqual(result['ending_cash'], 10.)
        self.assertFalse(result['ten_resolved_sequence_complete'])
        self.assertFalse(result['full_sequence_resolved'])

    def test_veto_precedes_cost_cash_and_quote_checks(self):
        rows = [fixtures.row(0, prediction=.01)]
        original = engine.entry_price_cap
        def cost(prediction, **kwargs):
            if prediction != .5:
                raise AssertionError('candidate cost cap reached before veto')
            return original(prediction, **kwargs)
        with patch.object(engine, 'entry_price_cap', side_effect=cost):
            result, source = play(rows, veto=lambda _: available(-.5))
        self.assertEqual(result['attempts'][0]['status'], 'decision_net_margin_below_floor')
        self.assertEqual(source.calls, [])
        self.assertEqual(result['attempts'][0]['cash_unchanged'], 10.)

    def test_threshold_equality_is_allowed_without_epsilon(self):
        row = fixtures.row(0)
        for margin, entered in ((.02, True), (np.nextafter(.02, -np.inf), False), (-.01, False)):
            with self.subTest(margin=margin):
                result, source = play([row], veto=lambda _, value=margin: available(value))
                self.assertEqual(result['fills'], int(entered))
                if entered:
                    evidence = result['chain'][0]['decision_veto_evidence']
                    self.assertIs(evidence['allowed'], True)
                else:
                    self.assertEqual(source.calls, [])
                    evidence = result['attempts'][0]['decision_veto_evidence']
                    self.assertIs(evidence['allowed'], False)
                self.assertEqual(evidence['predicted_net_margin_per_share'], margin)
                self.assertEqual(evidence['edge_floor'], .02)

    def test_unavailable_predictions_retain_each_status_no_zero_backfill(self):
        for status in ('not_fitted', 'unsupported', 'invalid_prediction'):
            with self.subTest(status=status):
                result, source = play([fixtures.row(0)], veto=lambda _, state=status: unavailable(state))
                attempt = result['attempts'][0]
                self.assertEqual(attempt['status'], 'decision_net_margin_unavailable')
                self.assertEqual(attempt['cash_unchanged'], 10.)
                self.assertEqual(attempt['decision_veto_evidence']['prediction_status'], status)
                self.assertIsNone(attempt['decision_veto_evidence']['predicted_net_margin_per_share'])
                self.assertEqual(source.calls, [])

    def test_only_readonly_nine_decision_fields_are_exposed(self):
        class Forbidden:
            def __bool__(self): raise AssertionError('future quality accessed')
            def __float__(self): raise AssertionError('future label accessed')
        row = fixtures.row(0)
        row.update(future_bid=Forbidden(), future_quality=Forbidden(), cash=Forbidden(), pending=Forbidden())
        seen = []
        def callback(candidate):
            self.assertIsInstance(candidate, MappingProxyType)
            self.assertEqual(set(candidate), set(engine.DECISION_VETO_FIELDS))
            self.assertNotIn('future_bid', candidate)
            self.assertNotIn('cash', candidate)
            self.assertNotIn('pending', candidate)
            self.assertIsInstance(candidate['decision_ts'], pd.Timestamp)
            self.assertEqual(candidate['predicted_exit_bid_price'], .8)
            with self.assertRaises(TypeError): candidate['predicted_exit_bid_price'] = 0.
            seen.append(dict(candidate))
            return available(.1)
        result, _ = play([row], veto=callback)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result['chain'][0]['predicted_exit_bid_price'], .8)

    def test_veto_does_not_release_or_change_pending_cash(self):
        first = fixtures.row(0)
        release = first['market_close_ts'] + pd.Timedelta(seconds=20)
        rejected = fixtures.row(1, decision=release + pd.Timedelta(seconds=1))
        accepted = fixtures.row(2, decision=release + pd.Timedelta(seconds=10))
        rows = [first, rejected, accepted]
        result, source = play(rows, veto=lambda row: available(-.1 if row['market_id'] == rejected['market_id'] else .1))
        first_event, second_event = result['chain']
        attempt = result['attempts'][0]
        self.assertEqual(attempt['status'], 'decision_net_margin_below_floor')
        self.assertEqual(attempt['cash_unchanged'], first_event['cash_after_entry'])
        self.assertEqual(second_event['cash_before_entry'], first_event['cash_after_entry'] + first_event['payout'])
        self.assertEqual(result['ending_cash'], second_event['cash_after_entry'] + second_event['payout'])
        self.assertFalse(any(mid == rejected['market_id'] for mid, _, _ in source.calls))
        self.assertEqual(result['processed_candidate_count'], 3)

    def test_veto_precedes_prior_cash_not_released_and_does_not_forget_pending(self):
        first = fixtures.row(0)
        release = first['market_close_ts'] + pd.Timedelta(seconds=20)
        rejected = fixtures.row(1, decision=first['decision_ts'] + pd.Timedelta(seconds=1))
        blocked = fixtures.row(2, decision=release - pd.Timedelta(seconds=3))
        later = fixtures.row(3, decision=release + pd.Timedelta(seconds=1))
        result, _ = play([first, rejected, blocked, later], veto=lambda row: available(-.1 if row['market_id'] == rejected['market_id'] else .1))
        self.assertEqual([a['status'] for a in result['attempts']], ['decision_net_margin_below_floor', 'prior_cash_not_released'])
        self.assertEqual(result['attempts'][0]['cash_unchanged'], result['attempts'][1]['cash_unchanged'])
        self.assertEqual(result['chain'][1]['cash_before_entry'], result['chain'][0]['cash_after_entry'] + result['chain'][0]['payout'])

    def test_allowed_veto_does_not_replace_original_cost_size_or_prediction(self):
        rows = [fixtures.row(0)]
        baseline, _ = play(rows)
        treatment, _ = play(rows, veto=lambda _: available(.8))
        evidence = treatment['chain'][0].pop('decision_veto_evidence')
        self.assertEqual(treatment['chain'], baseline['chain'])
        self.assertEqual(evidence['predicted_net_margin_per_share'], .8)
        self.assertEqual(treatment['ending_cash'], baseline['ending_cash'])
        bad_prediction = fixtures.row(1, prediction=.01)
        result, source = play([bad_prediction], veto=lambda _: available(.8))
        self.assertEqual(result['attempts'][0]['status'], 'decision_cost_floor_not_met')
        self.assertIn('decision_veto_evidence', result['attempts'][0])
        self.assertEqual(source.calls, [])

    def test_prefix_capture_uses_same_old_cash_state_and_calls_before_next_candidate(self):
        rows = [fixtures.row(i) for i in range(11)]
        old, _ = play(rows)
        snapshots = []
        seen = []
        def callback(row):
            if len(seen) == 10:
                self.assertEqual(len(snapshots), 1)
            seen.append(row['market_id'])
            return available(.1)
        actual, _ = play(rows, veto=callback, snapshots=snapshots)
        snapshot = deepcopy(snapshots[0])
        for event in snapshot['chain']: event.pop('decision_veto_evidence')
        self.assertEqual(snapshot, old['required_prefix_result'])
        self.assertEqual(actual['required_prefix_processed_candidate_count'], 10)
        self.assertEqual(actual['processed_candidate_count'], 11)

    def test_rejections_count_toward_prefix_processed_and_fewer_than_ten_are_valid(self):
        rows = [fixtures.row(i) for i in range(12)]
        snapshots = []
        result, _ = play(rows, veto=lambda row: available(-.1 if row['market_id'] in {rows[1]['market_id'], rows[2]['market_id'], rows[3]['market_id']} else .1), snapshots=snapshots)
        self.assertEqual(result['fills'], 9)
        self.assertEqual(result['resolved_fills'], 9)
        self.assertEqual(result['processed_candidate_count'], 12)
        self.assertIsNone(result['required_prefix_result'])
        self.assertEqual(snapshots, [])
        self.assertFalse(result['ten_resolved_sequence_complete'])
        result, _ = play(rows, veto=lambda row: available(-.1 if row['market_id'] == rows[1]['market_id'] else .1), snapshots=snapshots)
        self.assertEqual(result['required_prefix_processed_candidate_count'], 11)
        self.assertEqual(len(result['required_prefix_result']['attempts']), 1)

    def test_malformed_responses_nonfinite_bool_and_unknown_enums_rejected(self):
        bad = [None, {}, True, {'allow': True}, {**available(), 'extra': 1},
               available(True), available(np.bool_(False)), available(float('nan')), available(float('inf')),
               available(-float('inf')), available('.1'), available(10**10000),
               {'prediction_status': 'Available', 'predicted_net_margin_per_share': .1, 'reason': 'prediction_available'},
               {'prediction_status': 'available', 'predicted_net_margin_per_share': .1, 'reason': 'model_not_fitted'},
               {'prediction_status': 'available', 'predicted_net_margin_per_share': .1, 'reason': pd.NA},
               {'prediction_status': 'not_fitted', 'predicted_net_margin_per_share': 0., 'reason': 'model_not_fitted'}]
        for index, response in enumerate(bad):
            with self.subTest(index=index):
                source = fixtures.Source(fixtures.fixture([fixtures.row(0)]))
                with self.assertRaises(ValueError):
                    engine.replay_cached_quotes(pd.DataFrame([fixtures.row(0)]), ['toy'], source,
                        **fixtures.PARAMETERS, decision_veto=lambda _, value=response: value)
                self.assertEqual(source.calls, [])

    def test_noncallable_hook_rejected_before_quotes(self):
        source = fixtures.Source({})
        with self.assertRaisesRegex(ValueError, 'invalid_decision_veto'):
            engine.replay_cached_quotes(pd.DataFrame(), ['toy'], source, decision_veto={})
        self.assertEqual(source.calls, [])


if __name__ == '__main__':
    unittest.main()
