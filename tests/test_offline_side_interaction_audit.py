import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis import offline_side_interaction_audit as audit


def fixture(runs=None):
    runs = runs or ['past0', 'past1', 'past2', 'past3', 'current']
    parent = {'protocol_id': 'synthetic', 'evaluation_mode': 'book_bid_residual',
        'source_commit': 'a' * 40, 'expected_code_commit': 'b' * 40,
        'features': audit.FEATURES.copy(), 'source_runs': runs, 'run_order': runs,
        'expected_files_per_run': 1, 'minimum_prefix_quotes': 3, 'minimum_training_runs': 4,
        'minimum_training_labels': 100, 'evaluation_block_runs': 12, 'max_source_rows': 10000,
        'max_feature_rows': 1000, 'max_scored_runs': 10, 'max_replay_cells': 16,
        'opening_tolerance_seconds': 3., 'maximum_snapshot_age_seconds': 3.,
        'decision_cutoff_seconds': 120., 'entry_delay_seconds': 4., 'hold_seconds': 30.,
        'latency_seconds': 8., 'max_quote_delay_seconds': 3., 'edge_floor': .02,
        'fee_rate': .07, 'slippage': .01, 'depth_haircut': .1,
        'minimum_order_usd': 5., 'minimum_order_shares': 5., 'depth_thresholds': [3., 5., 7.],
        'cash_release_delay_after_close_seconds': 900., 'ridge_alpha': 1., 'ridge_solver': 'svd',
        'initial_cash': 10., 'required_fills': 10}
    rows = []
    for i, run in enumerate(runs):
        for j in range(25 if i < 4 else 3):
            start = pd.Timestamp('2026-01-01T00:00:00Z') + pd.Timedelta(days=i, seconds=300 * j)
            slug = 'btc-updown-5m-' + str(int(start.timestamp()))
            unknown = i == 4 and j == 2
            row = {'market_id': run + '::' + slug, 'run_name': run, 'slug': slug,
                'side': 'up' if j % 2 == 0 else 'down', 'decision_ts': start + pd.Timedelta(seconds=120),
                'market_close_ts': start + pd.Timedelta(seconds=300),
                'decision_ask_price': .42, 'decision_bid_price': .40, 'decision_sell_size': 1000.,
                'future_bid_price': np.nan if unknown else .45,
                'bid_residual_target': np.nan if unknown else .05,
                'label_available_ts': pd.NaT if unknown else start + pd.Timedelta(seconds=170),
                'target_status': 'unknown' if unknown else 'known_future_bid'}
            row.update({name: float(j + 1) for name in audit.FEATURES})
            rows.append(row)
    frame = pd.DataFrame(rows)
    plans, cohort = audit.build_fit_plan(frame, parent)
    control = cohort.iloc[0]
    projection = [{key: str(control[key]) if key != 'predicted_exit_bid_price' else '.8'
                   for key in audit.CANDIDATE_FIELDS}]
    test_runs = runs[4:]
    results = [{'scope': 'pooled_single_ledger' if index == 0 else 'disjoint_diagnostic_block',
        'block_index': index, 'runs': test_runs, 'slippage': .01, 'latency_seconds': 8., 'depth_haircut': .1,
        'fills': 0, 'resolved_fills': 0, 'ending_cash': 10., 'locked_shares': 0.,
        'ending_cash_available_ts': None, 'chain': [], 'attempts': [],
        'failure_reason': 'sequence_or_net_growth_gate_not_met'} for index in range(2)]
    metrics = audit.base.prediction_metrics(cohort.assign(predicted_exit_bid_price=.4))
    bindings = {key: 'd' * 64 for key in ('summary_sha256', 'scores_csv_sha256', 'model_parameters_sha256', 'candidates_csv_sha256')}
    bindings['protocol_sha256'] = hashlib.sha256(audit.canonical(parent)).hexdigest()
    bindings['unchanged_code_file_sha256'] = {name: audit.file_sha256(audit.CURRENT_DIR.parent / name) for name in audit.UNCHANGED_FILES}
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        audit.base.storage._private_csv(folder / 'features.csv', frame)
        bindings['features_csv_sha256'] = audit.file_sha256(folder / 'features.csv')
        member_parts = []
        for plan in plans:
            train = frame.loc[plan['train_index'], ['market_id', 'label_available_ts', 'bid_residual_target']].copy()
            train.insert(0, 'fit_run', plan['reference']['run_name'])
            train['decision_cutoff_ts'] = pd.Timestamp(plan['reference']['decision_cutoff_ts'])
            member_parts.append(train)
        audit.base.storage._private_csv(folder / 'members.csv', pd.concat(member_parts, ignore_index=True))
        bindings['training_members_csv_sha256'] = audit.file_sha256(folder / 'members.csv')
    config = {'protocol_id': 'synthetic_signed', 'evaluation_mode': 'paired_side_interaction',
        'source_commit': 'a' * 40, 'expected_code_commit': 'c' * 40, 'parent_code_commit': 'b' * 40,
        'base_protocol': parent, 'parent_bindings': bindings, 'treatment': copy.deepcopy(audit.TREATMENT),
        'control_model_refit_allowed': False, 'live_orders_enabled': False, 'actual_fills': 0,
        'control_result_fingerprint_version': audit.FINGERPRINT_VERSION,
        'max_new_model_fit_calls': 5, 'max_new_model_predict_calls': 5, 'max_replay_cells': 16,
        'max_private_config_bytes': 262144, 'expected_control_candidate_count': 1,
        'expected_control_result_count': 2, 'expected_control_result_fingerprints': [audit.result_fingerprint(r) for r in results],
        'expected_parent_fit_records': [plan['reference'] for plan in plans],
        'expected_scored_cohort': audit.cohort_contract(cohort),
        'expected_scored_cohort_per_run': {str(run): audit.cohort_contract(part) for run, part in cohort.groupby('run_name',sort=False)},
        'cached_parent_prediction_reference_kind': 'audited_cached_score_metrics_not_refitted_or_fully_rescored_control',
        'cached_parent_prediction_metrics': metrics, 'evaluation_class': 'synthetic_only',
        'frozen_control_candidate_fields': audit.CANDIDATE_FIELDS.copy(),
        'frozen_control_candidates': projection,
        'frozen_control_candidate_rows_sha256': hashlib.sha256(audit.canonical(projection)).hexdigest()}
    return config, frame, results


def fake_fit(train, current, parent):
    out = current.copy()
    out['predicted_oriented_residual_raw'] = .2 * np.where(out.side.eq('up'), 1., -1.)
    out['predicted_bid_residual_raw'] = .2
    out['predicted_exit_bid_unclipped'] = out.decision_bid_price + .2
    out['predicted_exit_bid_price'] = out.predicted_exit_bid_unclipped.clip(0, 1)
    return out, {'scaler_mean': [0.] * len(audit.FEATURES), 'scaler_scale': [1.] * len(audit.FEATURES),
                 'ridge_coef': [0.] * len(audit.FEATURES), 'ridge_intercept': 0., 'orientation': {}}


class SideInteractionAuditTests(unittest.TestCase):
    def run_synthetic(self, config, frame, results, fit_side_effect=fake_fit, source_failure=None, books=None, replay_real=False):
        original_replay = audit.replay_arm
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(audit, 'verified_inputs', side_effect=source_failure,
                    return_value={'source_commit': config['source_commit'], 'code_commit': config['expected_code_commit']}) as verify, \
                patch.object(audit.base, 'prepare_archive', return_value=(frame, books or {}, {'admitted_markets': len(frame)})) as prepare, \
                patch.object(audit, 'replay_arm', return_value=results, side_effect=original_replay if replay_real else None) as replay, \
                patch.object(audit, 'fit_predict_once', side_effect=fit_side_effect) as fit, \
                patch.object(audit.base, 'fit_predict_once', side_effect=AssertionError('control model must not execute')):
            output = Path(tmp) / 'result'
            result = audit.evaluate(Path(tmp) / 'never-read-source', config, output)
            saved = json.loads((output / 'summary.json').read_text())
            self.assertEqual(saved['evaluation_status'], result['evaluation_status'])
            scores = pd.read_csv(output / 'scores.csv') if (output / 'scores.csv').exists() else None
            members = pd.read_csv(output / 'training-members.csv') if (output / 'training-members.csv').exists() else None
            return result, {'verify': verify.call_count, 'prepare': prepare.call_count, 'replay': replay.call_count, 'fit': fit.call_count}, scores, members

    def test_complete_guards_precede_single_treatment_and_unknown_is_retained(self):
        config, frame, results = fixture()
        result, calls, scores, members = self.run_synthetic(config, frame, results)
        self.assertEqual(result['evaluation_status'], 'evaluated', result.get('failure'))
        self.assertTrue(result['comparison_valid'])
        self.assertTrue(result['control_execution_reproduced'])
        self.assertEqual(calls, {'verify': 1, 'prepare': 1, 'replay': 2, 'fit': 1})
        self.assertEqual(result['actual_model_fit_calls'], 1)
        self.assertEqual(result['actual_model_predict_calls'], 1)
        self.assertEqual(result['control_model_fit_calls'], 0)
        self.assertEqual(result['control_model_predict_calls'], 0)
        self.assertFalse(result['control_full_predictions_reproduced'])
        self.assertEqual(len(scores), 3)
        self.assertEqual(scores.future_bid_price.isna().sum(), 1)
        self.assertEqual(result['arms']['side_interaction']['candidate_rows'], 3)
        self.assertEqual(list(members), audit.MEMBER_FIELDS)
        self.assertEqual(len(members), 100)
        self.assertEqual(result['fit_records'][0]['training_known_runs'], 4)
        self.assertTrue(all(g['passed'] for g in result['pre_fit_gates']))
        paired = result['predictive_comparison']['paired_per_run'][0]
        self.assertEqual(paired['known_targets'], 2)
        self.assertAlmostEqual(paired['treatment_mse'], .15 ** 2)
        self.assertAlmostEqual(paired['cached_control_mse'], .05 ** 2)
        self.assertAlmostEqual(paired['persistence_mse'], .05 ** 2)
        self.assertAlmostEqual(paired['cached_control_sse_delta'], .04)
        for group in result['predictive_comparison']['side_diagnostics'].values():
            self.assertIsNone(group['cached_control_by_side'])

    def test_source_helper_gate_precedes_all_archive_reads_and_model_calls(self):
        config, frame, results = fixture()
        result, calls, _, _ = self.run_synthetic(config, frame, results, source_failure=ValueError('immutable_parent_helper_bytes_changed'))
        self.assertEqual(result['evaluation_status'], 'comparison_invalid')
        self.assertEqual(calls['prepare'], 0)
        self.assertEqual(calls['fit'], 0)
        self.assertEqual(result['actual_model_fit_calls'], 0)

    def test_feature_byte_mismatch_precedes_membership_replay_and_fit(self):
        config, frame, results = fixture()
        config['parent_bindings']['features_csv_sha256'] = '0' * 64
        result, calls, _, _ = self.run_synthetic(config, frame, results)
        self.assertEqual(result['failure']['reason'], 'parent_features_csv_sha256_mismatch')
        self.assertEqual(calls['replay'], 0)
        self.assertEqual(calls['fit'], 0)
        self.assertFalse(result['strategy_evaluated'])

    def test_all_member_cutoff_and_known_unknown_identity_guards_precede_fit(self):
        edits = [lambda c: c['expected_parent_fit_records'][0].update(decision_cutoff_ts='2026-02-01T00:00:00+00:00'),
                 lambda c: c['expected_parent_fit_records'][0].update(training_market_ids_sha256='0' * 64),
                 lambda c: c['expected_scored_cohort'].update(unknown_ids_sha256='0' * 64),
                 lambda c: c['expected_scored_cohort_per_run']['current'].update(known_ids_sha256='0' * 64),
                 lambda c: c['parent_bindings'].update(training_members_csv_sha256='0' * 64)]
        for edit in edits:
            with self.subTest(edit=edit):
                config, frame, results = fixture(); edit(config)
                result, calls, _, _ = self.run_synthetic(config, frame, results)
                self.assertEqual(result['evaluation_status'], 'comparison_invalid')
                self.assertEqual(calls['fit'], 0)
                self.assertEqual(calls['replay'], 0)

    def test_full_control_attempt_fingerprint_mismatch_prevents_fit(self):
        config, frame, results = fixture()
        results[0]['attempts'] = [{'market_id': 'synthetic', 'status': 'entry_quote_unavailable', 'cash_unchanged': 10.}]
        result, calls, _, _ = self.run_synthetic(config, frame, results)
        self.assertEqual(calls['replay'], 1)
        self.assertEqual(calls['fit'], 0)
        self.assertFalse(result['control_execution_reproduced'])
        self.assertEqual(result['failure']['reason'], 'full_control_result_fingerprints_mismatch')

    def test_projection_hash_and_parent_protocol_are_not_silently_rebound(self):
        for change in ('candidate', 'parent'):
            with self.subTest(change=change):
                config, frame, results = fixture()
                if change == 'candidate':
                    config['frozen_control_candidates'][0]['predicted_exit_bid_price'] = '.9'
                else:
                    config['base_protocol']['expected_code_commit'] = config['expected_code_commit']
                result, calls, _, _ = self.run_synthetic(config, frame, results)
                self.assertEqual(result['failure']['stage'], 'configuration')
                self.assertEqual(calls['prepare'], 0)
                self.assertEqual(calls['fit'], 0)

    def test_failure_after_actual_model_call_preserves_call_evidence(self):
        config, frame, results = fixture()
        failure = RuntimeError('synthetic_predict_failure')
        failure.audit_fit_calls = 1; failure.audit_predict_calls = 1
        result, _, _, _ = self.run_synthetic(config, frame, results, fit_side_effect=failure)
        self.assertEqual(result['evaluation_status'], 'evaluation_failed')
        self.assertTrue(result['control_execution_reproduced'])
        self.assertFalse(result['strategy_evaluated'])
        self.assertEqual(result['actual_model_fit_calls'], 1)
        self.assertEqual(result['actual_model_predict_calls'], 1)
        self.assertEqual(result['fit_records'][0]['status'], 'model_call_failed')

    def test_actual_synthetic_quote_change_invalidates_control_before_fitting(self):
        config, frame, _ = fixture()
        control = audit.frozen_candidates(config)
        row = control.iloc[0]
        arrival = row.decision_ts + pd.Timedelta(seconds=12)
        book = pd.DataFrame({'ts_utc': [arrival, arrival + pd.Timedelta(seconds=38)],
            'buy_up_cents': [42., 47.], 'buy_up_size': [1000., 1000.],
            'sell_up_cents': [40., 45.], 'sell_up_size': [1000., 1000.], 'quote_conflict': [False, False]})
        original = audit.replay_arm(control, {row.market_id: book}, config['base_protocol'])
        config['expected_control_result_fingerprints'] = [audit.result_fingerprint(cell) for cell in original]
        changed = book.copy(); changed.loc[1, 'sell_up_size'] = .1
        result, calls, _, _ = self.run_synthetic(config, frame, original,
            books={row.market_id: changed}, replay_real=True)
        self.assertEqual(result['failure']['reason'], 'full_control_result_fingerprints_mismatch')
        self.assertEqual(calls['fit'], 0)
        self.assertEqual(result['arms']['frozen_control']['results'][0]['chain'][0]['status'], 'unresolved_position')

    def test_model_postprocessing_failure_preserves_fit_and_prediction_calls(self):
        class BrokenParameters:
            def fit(self, *_):
                return self
            def predict(self, values):
                return np.zeros(len(values))
            @property
            def steps(self):
                raise RuntimeError('synthetic_parameter_serialization_failure')
        config, frame, results = fixture()
        with patch.object(audit, 'make_pipeline', return_value=BrokenParameters()):
            result, _, _, _ = self.run_synthetic(config, frame, results, fit_side_effect=audit.fit_predict_once)
        self.assertEqual(result['evaluation_status'], 'evaluation_failed')
        self.assertEqual(result['actual_model_fit_calls'], 1)
        self.assertEqual(result['actual_model_predict_calls'], 1)
        self.assertEqual(result['failure']['reason'], 'synthetic_parameter_serialization_failure')

    def test_later_model_failure_retains_prior_scores_and_physical_members(self):
        config, frame, results = fixture(['past0', 'past1', 'past2', 'past3', 'current', 'next'])
        calls_seen = []
        def fail_second(train, current, parent):
            calls_seen.append(len(current))
            if len(calls_seen) == 2:
                exc = RuntimeError('synthetic_second_fit_failure')
                exc.audit_fit_calls = 1; exc.audit_predict_calls = 0
                raise exc
            return fake_fit(train, current, parent)
        result, _, scores, members = self.run_synthetic(config, frame, results, fit_side_effect=fail_second)
        self.assertEqual(result['evaluation_status'], 'evaluation_failed')
        self.assertEqual(result['actual_model_fit_calls'], 2)
        self.assertEqual(result['actual_model_predict_calls'], 1)
        self.assertEqual(result['model_scored_rows'], 3)
        self.assertEqual(result['fitted_scored_runs'], 1)
        self.assertEqual(len(scores), 3)
        self.assertEqual(len(members), 202)
        self.assertEqual(result['fit_records'][0]['status'], 'fit_with_prior_available_labels')

    def test_round10_fingerprint_preserves_every_field_and_numeric_type(self):
        cell = {'chain': [{'fee': .1234567890123}], 'attempts': [{'reason': 'first'}], 'cash': 0.0, 'negative_zero': -.0}
        expected = {'chain': [{'fee': round(.1234567890123, 10)}], 'attempts': [{'reason': 'first'}], 'cash': 0.0, 'negative_zero': -.0}
        self.assertEqual(audit.result_fingerprint(cell), hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest())
        changed = copy.deepcopy(cell); changed['attempts'][0]['reason'] = 'second'
        self.assertNotEqual(audit.result_fingerprint(cell), audit.result_fingerprint(changed))
        changed = copy.deepcopy(cell); changed['cash'] = 0
        self.assertNotEqual(audit.result_fingerprint(cell), audit.result_fingerprint(changed))
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            audit.result_fingerprint({'cash': float('nan')})


if __name__ == '__main__':
    unittest.main()
