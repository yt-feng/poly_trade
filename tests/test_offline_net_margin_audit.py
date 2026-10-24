import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from analysis import offline_net_margin_audit as audit

spec = importlib.util.spec_from_file_location('net_margin_synthetic_continuation', Path(__file__).with_name('test_offline_exit_continuation_audit.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def synthetic_training(candidate_rows, base):
    rows = []
    first_decision = pd.Timestamp(candidate_rows[0]['decision_ts'])
    past = {**candidate_rows[0], 'market_id': 'past::distinct-past-market', 'run_name': 'past', 'slug': 'distinct-past-market',
        'decision_ts': first_decision - pd.Timedelta(days=1), 'market_close_ts': first_decision - pd.Timedelta(days=1) + pd.Timedelta(seconds=180)}
    for source in [past, *candidate_rows]:
        row = {**source, **{name: .5 for name in audit.FEATURES}}
        row['decision_ts'] = pd.Timestamp(source['decision_ts'])
        row['market_close_ts'] = pd.Timestamp(source['market_close_ts'])
        row.update(first_quote_ts=row['decision_ts'] - pd.Timedelta(seconds=120), decision_bid_price=.49,
            decision_ask_price=.5, decision_sell_size=1000., future_bid_price=.6, bid_residual_target=.11,
            target_entry_quote_ts=row['decision_ts'] + pd.Timedelta(seconds=12),
            target_exit_arrival_ts=row['decision_ts'] + pd.Timedelta(seconds=50),
            label_available_ts=row['decision_ts'] + pd.Timedelta(seconds=50), target_status='known')
        rows.append(row)
    frame = pd.DataFrame(rows)
    domains = []
    for _, row in frame.iterrows():
        entry = {'ts_utc': row.target_entry_quote_ts}
        exit_ = {'ts_utc': row.target_exit_arrival_ts}
        for side in ('up', 'down'):
            entry.update({f'buy_{side}_cents': 50., f'sell_{side}_cents': 49., f'buy_{side}_size': 1000., f'sell_{side}_size': 1000.})
            exit_.update({f'buy_{side}_cents': 61., f'sell_{side}_cents': 60., f'buy_{side}_size': 1000., f'sell_{side}_size': 1000.})
        domains.append(audit.quality.domains_export.export_quote_row(row, pd.DataFrame([entry, exit_]), base))
    domains = pd.DataFrame(domains, columns=audit.quality.domains_export.COLUMNS).astype(object).fillna('')
    frame['quality_label_observed'] = True
    frame['capacity_log_target'] = np.log1p(100.)
    return frame, domains


class NativeTargetTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.fixtures.protocol()
        self.frame, self.domains = synthetic_training([fixture.fixtures.candidate()], self.base)

    def test_literal_signed_margin_and_cost_clock(self):
        self.domains.loc[0, 'exit_bid_price'] = .30
        p_in, p_out = .51, .29
        c_in = .51 + .07 * .51 * .49
        c_out = .29 - .07 * .29 * .71
        self.domains.loc[0, ['entry_fill_price', 'exit_fill_price', 'entry_cash_cost_per_share', 'exit_net_proceeds_per_share', 'net_cash_difference_per_share']] = [p_in, p_out, c_in, c_out, c_out - c_in]
        out = audit.add_native_margin_targets(self.frame, self.domains, self.base)
        self.assertAlmostEqual(out.iloc[0].net_cash_difference_per_share, -.251906)
        self.assertLess(out.iloc[0].net_cash_difference_per_share, 0.)
        self.assertEqual(list(out.market_id), list(self.frame.market_id))
        self.assertEqual(out.iloc[0].cash_domain_label_available_ts, self.frame.iloc[0].label_available_ts)

    def test_every_consumed_clock_and_algebra_change_is_rejected(self):
        mutations = [('entry_target_ts', '2026-01-01T00:00:00Z'), ('exit_target_ts', '2026-01-01T00:00:00Z'),
            ('cash_domain_label_available_ts', self.domains.iloc[0].entry_quote_ts),
            ('entry_fill_price', .7), ('exit_fill_price', .7), ('entry_cash_cost_per_share', .7),
            ('exit_net_proceeds_per_share', .7), ('net_cash_difference_per_share', 0.)]
        for key, value in mutations:
            with self.subTest(key=key):
                changed = self.domains.copy(deep=True)
                changed.loc[0, key] = value
                with self.assertRaises(ValueError):
                    audit.add_native_margin_targets(self.frame, changed, self.base)

    def test_unknown_retained_with_nullable_margin_and_clock(self):
        frame, domains = self.frame.copy(), self.domains.copy()
        frame.loc[0, 'quality_label_observed'] = False
        fields = ['entry_fill_price', 'exit_fill_price', 'entry_cash_cost_per_share', 'exit_net_proceeds_per_share',
                  'net_cash_difference_per_share', 'cash_domain_label_available_ts']
        domains.loc[0, fields] = ''
        out = audit.add_native_margin_targets(frame, domains, self.base)
        self.assertEqual(len(out), len(frame))
        self.assertTrue(pd.isna(out.iloc[0].net_cash_difference_per_share))
        self.assertTrue(pd.isna(out.iloc[0].cash_domain_label_available_ts))
        domains.loc[0, 'net_cash_difference_per_share'] = 0.
        with self.assertRaisesRegex(ValueError, 'nullable'):
            audit.add_native_margin_targets(frame, domains, self.base)

    def test_strict_equality_cutoff_excludes_and_nanosecond_past_includes(self):
        frame = audit.add_native_margin_targets(self.frame, self.domains, self.base)
        cutoff = frame.loc[1, 'decision_ts']
        for name in ('label_available_ts', 'cash_domain_label_available_ts'):
            frame[name] = frame[name].astype('datetime64[ns, UTC]')
        frame.loc[0, ['label_available_ts', 'cash_domain_label_available_ts']] = cutoff
        plan = audit.native_training_plans(frame, self.base)[0]
        self.assertEqual(len(plan['train_index']), 0)
        frame.loc[0, ['label_available_ts', 'cash_domain_label_available_ts']] = cutoff - pd.Timedelta(nanoseconds=1)
        plan = audit.native_training_plans(frame, self.base)[0]
        self.assertEqual(list(plan['train_index']), [0])

    def test_current_future_quality_or_target_cannot_change_past_training_members(self):
        frame = audit.add_native_margin_targets(self.frame, self.domains, self.base)
        original = audit.native_training_plans(frame, self.base)[0]
        changed = frame.copy(deep=True)
        changed.loc[1, 'net_cash_difference_per_share'] = -999.
        changed.loc[1, 'quality_label_observed'] = False
        revised = audit.native_training_plans(changed, self.base)[0]
        self.assertEqual(list(revised['train_index']), list(original['train_index']))
        self.assertEqual(list(revised['test_index']), list(original['test_index']))
        self.assertEqual(revised['reference'], original['reference'])

    def test_cash_label_clock_cannot_differ_from_training_mask_clock(self):
        frame = audit.add_native_margin_targets(self.frame, self.domains, self.base)
        frame.loc[0, 'cash_domain_label_available_ts'] = frame.loc[1, 'decision_ts']
        with self.assertRaisesRegex(ValueError, 'future_or_unknown'):
            audit.native_training_plans(frame, self.base)

    def test_output_adapter_never_side_or_bid_maps_or_clips(self):
        current = pd.DataFrame({'side': ['up', 'down', 'down'], 'decision_bid_price': [.1, .9, .5]})
        values = np.array([-.3, -.4, 1.2])
        out = audit.apply_native_outputs(current, values)
        np.testing.assert_array_equal(out.predicted_net_margin_per_share, values)
        self.assertEqual(list(out.side), ['up', 'down', 'down'])
        self.assertNotIn('predicted_exit_bid_price', out)
        for invalid in ([1., 2.], [1., np.nan, 2.]):
            with self.assertRaises(ValueError):
                audit.apply_native_outputs(current, invalid)

    def test_native_fit_uses_literal_signed_targets_exactly_once_and_train_only_scaler(self):
        frame = audit.add_native_margin_targets(self.frame, self.domains, self.base)
        frame['side'] = ['down', 'up']
        frame['net_cash_difference_per_share'] = [-.251906, .123]
        frame.loc[0, audit.FEATURES[0]] = 2.
        frame.loc[1, audit.FEATURES[0]] = 100.
        cfg = {'training_binding': {'price_model_parameters': audit.PARAMETERS},
               'parent_execution_config': {'parent_strategy_config': {'base_protocol': self.base}}}
        model = Mock()
        model.get_params.return_value = audit.PARAMETERS
        model.predict.return_value = np.array([-.251906])
        with patch.object(audit, 'HistGradientBoostingRegressor', return_value=model), \
             patch.object(audit.quality, 'assert_runtime_environment', return_value={}), \
             patch.object(audit.quality, 'serialize_histogram_model', return_value={'histogram_model': {}}) as serializer, \
             patch.object(audit.quality, 'reconstruct_serialized_histogram', return_value=np.array([-.251906])):
            raw, artifact = audit.fit_predict_once(frame.iloc[:1], frame.iloc[1:], cfg)
        np.testing.assert_array_equal(model.fit.call_args.args[1], [-.251906])
        self.assertEqual(model.fit.call_count, 1)
        self.assertEqual(model.predict.call_count, 1)
        np.testing.assert_array_equal(model.fit.call_args.args[0][0], np.zeros(len(audit.FEATURES)))
        self.assertEqual(model.predict.call_args.args[0][0][0], 98.)
        self.assertEqual(artifact['model_kind'], 'net_margin')
        self.assertEqual(artifact['output_map'], audit.NATIVE_OUTPUT_MAP)
        np.testing.assert_array_equal(raw, [-.251906])
        self.assertEqual(serializer.call_args.args[-1], 'price')

    def test_failed_fit_and_predict_counters_survive(self):
        frame = audit.add_native_margin_targets(self.frame, self.domains, self.base)
        cfg = {'training_binding': {'price_model_parameters': audit.PARAMETERS},
               'parent_execution_config': {'parent_strategy_config': {'base_protocol': self.base}}}
        for phase in ('fit', 'predict'):
            model = Mock()
            model.get_params.return_value = audit.PARAMETERS
            if phase == 'fit':
                model.fit.side_effect = ValueError('synthetic_fit_failure')
            else:
                model.predict.side_effect = ValueError('synthetic_predict_failure')
            with patch.object(audit, 'HistGradientBoostingRegressor', return_value=model), patch.object(audit.quality, 'assert_runtime_environment', return_value={}):
                with self.assertRaises(ValueError) as caught:
                    audit.fit_predict_once(frame.iloc[:1], frame.iloc[1:], cfg)
            self.assertEqual(caught.exception.audit_fit_calls, 1)
            self.assertEqual(caught.exception.audit_predict_calls, int(phase == 'predict'))


class NetMarginAuditTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.ContinuationAuditTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.root = self.h.root
        expected = self.h.run_continuation(self.root / 'parent')
        self.expected = expected
        self.frame, self.domains = synthetic_training(self.h.rows, self.h.base)
        cohort = self.frame[self.frame.run_name.eq('run')]
        environment = {**self.h.parent['environment'], 'must_assert_effective_get_params_exact_before_fit': True,
            'must_assert_runtime_versions_before_model_construction': True, 'must_record_loaded_histogram_implementation_module_file_hashes': True}
        private = {name: {'descriptor_sha256': 'a' * 64, 'source': {'repository': 'synthetic/vault', 'commit': 'd' * 40,
                   'path': 'research_vault/episodes/' + 'e' * 64 + '/part-0000.vault'}} for name in ('dataset', 'control')}
        binding = {'cache_bindings': {name: 'a' * 64 for name in ('features_csv_sha256', 'domains_csv_sha256', 'control_predictions_csv_sha256')},
            'expected_all_cohort': audit.quality.cohort_contract(self.frame), 'expected_test_cohort': audit.quality.cohort_contract(cohort),
            'expected_test_cohort_per_run': {'run': audit.quality.cohort_contract(cohort)}, 'private_inputs': private,
            'environment': environment, 'price_model_parameters': copy.deepcopy(audit.PARAMETERS),
            'model_artifact_contract': copy.deepcopy(audit.quality.MODEL_ARTIFACT_CONTRACT), 'model_contract': copy.deepcopy(audit.quality.MODEL_CONTRACT)}
        self.config = {'evaluation_mode': 'offline_net_margin', 'parent_execution_config': copy.deepcopy(self.h.config),
            'parent_execution_config_sha256': audit.prior.digest(audit.prior.canonical(self.h.config)),
            'source_commit': self.h.config['source_commit'], 'source_repository': self.h.config['source_repository'],
            'candidate_binding': copy.deepcopy(self.h.binding), 'expected_code_commit': 'f' * 40,
            'code_file_sha256': {k: 'e' * 64 for k in audit.REQUIRED_CODE_FILES}, 'training_binding': binding,
            'training_binding_sha256': audit.prior.digest(audit.prior.canonical(binding)),
            'baseline_binding': {'expected_full_pooled_fingerprint': audit.prior.fingerprints.result_fingerprint(expected['full_ledger'])},
            'margin_policy': dict(audit.POLICY), 'max_producer_replays': 2, 'max_model_fit_calls': 1, 'max_model_predict_calls': 1,
            'max_new_cloud_runs': 1, 'control_model_refit_allowed': False, 'control_model_predict_allowed': False,
            'capacity_model_fit_allowed': False, 'capacity_model_predict_allowed': False, 'new_data_collection_allowed': False,
            'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False}

    def evaluate(self, fit_values=None, name='out'):
        def fake_fit(train, current, config):
            values = np.full(len(current), .2) if fit_values is None else np.asarray(fit_values)
            return values, {'model_kind': 'net_margin', 'synthetic_stub': True}
        with patch.object(audit.prior, 'verified_code_source', return_value={}), \
             patch.object(audit.quality, 'assert_runtime_environment', return_value={}), \
             patch.object(audit.quality, 'load_inputs', return_value=(self.frame, self.domains, pd.DataFrame())) as inputs, \
             patch.object(audit, 'fit_predict_once', side_effect=fake_fit) as fitted:
            result = audit.evaluate(self.h.source, self.h.candidate_path, self.root / 'dataset', self.root / 'control', self.config, self.root / name)
        return result, fitted.call_count, inputs.call_count

    def test_exact_baseline_then_one_learner_then_one_unchanged_candidate_replay(self):
        original = audit.prior.execution.replay_cached_quotes
        with patch.object(audit.prior.execution, 'replay_cached_quotes', wraps=original) as replay:
            result, fits, loads = self.evaluate()
        self.assertEqual((fits, loads, replay.call_count), (1, 1, 2))
        self.assertEqual(result['arms'][audit.BASE_ARM], self.expected['full_ledger'])
        self.assertEqual(result['prefix_ledgers'][audit.BASE_ARM], self.expected['prefix_ledger'])
        self.assertNotIn('decision_veto', replay.call_args_list[0].kwargs)
        self.assertIn('decision_veto', replay.call_args_list[1].kwargs)
        pd.testing.assert_frame_equal(replay.call_args_list[0].args[0], replay.call_args_list[1].args[0])
        self.assertEqual(result['actual_model_fit_calls'], 1)
        self.assertEqual(result['actual_model_predict_calls'], 1)
        self.assertEqual(result['model_tree_reconstructions'], 1)
        self.assertEqual(result['model_scored_rows'], len(self.h.rows))
        self.assertEqual(result['model_forecast_rows'], len(self.h.rows))
        self.assertEqual(result['gate_rows'], len(self.h.rows))
        self.assertEqual(result['producer_replay_completions'], 2)
        self.assertTrue(result['comparison_valid'])
        self.assertEqual(set(p.name for p in (self.root / 'out').iterdir()), audit.OUTPUTS)
        self.assertEqual(result['capacity_model_fit_calls'], 0)
        self.assertEqual(result['control_model_predict_calls'], 0)
        scores = pd.read_csv(self.root / 'out/scores.csv')
        self.assertEqual(list(scores), audit.SCORE_FIELDS)
        self.assertEqual(list(scores.market_id), [row['market_id'] for row in self.h.rows])

    def test_negative_margin_rejects_all_original_candidates_without_any_treatment_quote(self):
        result, fits, _ = self.evaluate(np.full(len(self.h.rows), -.25))
        treatment = result['arms'][audit.TREATMENT_ARM]
        self.assertEqual(fits, 1)
        self.assertEqual(treatment['fills'], 0)
        self.assertEqual(treatment['ending_cash'], 10.)
        self.assertEqual(len(treatment['attempts']), len(self.h.rows))
        self.assertEqual(treatment['processed_candidate_count'], len(self.h.rows))
        self.assertTrue(all(row['status'] == 'decision_net_margin_below_floor' for row in treatment['attempts']))
        self.assertEqual(result['quote_requests'][audit.TREATMENT_ARM], [])
        self.assertIsNone(result['prefix_ledgers'][audit.TREATMENT_ARM])
        self.assertFalse(result['arm_endpoints'][audit.TREATMENT_ARM]['full_economic_pass'])
        gates = [json.loads(line) for line in (self.root / 'out/candidate-gate-predictions.jsonl').read_text().splitlines()]
        self.assertEqual(len(gates), len(self.h.rows))
        self.assertTrue(all(row['predicted_net_margin_per_share'] == -.25 for row in gates))

    def test_insufficient_training_preserves_all_test_rows_nullable_no_backfill(self):
        self.frame.loc[0, 'quality_label_observed'] = False
        fields = ['entry_fill_price', 'exit_fill_price', 'entry_cash_cost_per_share', 'exit_net_proceeds_per_share',
                  'net_cash_difference_per_share', 'cash_domain_label_available_ts']
        self.domains.loc[0, fields] = ''
        result, fits, _ = self.evaluate()
        self.assertEqual(fits, 0)
        self.assertEqual(result['actual_model_fit_calls'], 0)
        self.assertEqual(result['model_scored_rows'], len(self.h.rows))
        self.assertEqual(result['model_forecast_rows'], 0)
        self.assertEqual(result['gate_rows'], len(self.h.rows))
        scores = pd.read_csv(self.root / 'out/scores.csv')
        self.assertEqual(len(scores), len(self.h.rows))
        self.assertTrue(scores.predicted_net_margin_per_share.isna().all())
        self.assertTrue(all(row['status'] == 'decision_net_margin_unavailable' for row in result['arms'][audit.TREATMENT_ARM]['attempts']))
        self.assertEqual((self.root / 'out/net-margin-models.jsonl').read_bytes(), b'')

    def test_typed_scoped_loader_with_three_real_synthetic_csvs(self):
        dataset, controls = self.root / 'dataset', self.root / 'control'
        dataset.mkdir()
        controls.mkdir()
        audit.quality.base.storage._private_csv(dataset / 'features.csv', self.frame)
        audit.quality.base.storage._private_csv(dataset / 'quote-cash-domains.csv', self.domains)
        cohort = self.frame[self.frame.run_name.eq('run')]
        control = cohort[['market_id', 'run_name']].copy()
        control['fit_run'] = control.run_name
        control['predicted_oriented_residual_raw'] = .31
        control['predicted_bid_residual_raw'] = .31
        control['predicted_exit_bid_unclipped'] = .8
        control['predicted_exit_bid_price'] = .8
        audit.quality.base.storage._private_csv(controls / 'control-predictions.csv', control[audit.quality.CONTROL_COLUMNS])
        self.config['training_binding']['cache_bindings'] = {
            'features_csv_sha256': audit.quality.file_sha256(dataset / 'features.csv'),
            'domains_csv_sha256': audit.quality.file_sha256(dataset / 'quote-cash-domains.csv'),
            'control_predictions_csv_sha256': audit.quality.file_sha256(controls / 'control-predictions.csv')}
        self.config['training_binding_sha256'] = audit.prior.digest(audit.prior.canonical(self.config['training_binding']))
        with patch.object(audit.prior, 'verified_code_source', return_value={}), \
             patch.object(audit.quality, 'assert_runtime_environment', return_value={}), \
             patch.object(audit, 'fit_predict_once', side_effect=lambda train, current, cfg: (np.full(len(current), .2), {'synthetic_stub': True})) as fitted:
            result = audit.evaluate(self.h.source, self.h.candidate_path, dataset, controls, self.config, self.root / 'out')
        self.assertEqual(fitted.call_count, 1)
        self.assertTrue(result['comparison_valid'])
        self.assertEqual(result['model_scored_rows'], len(cohort))
        self.assertEqual(result['all_test_cohort'], self.config['training_binding']['expected_test_cohort'])

    def test_candidate_veto_uses_no_future_quality_and_retains_explicit_unavailable(self):
        frame = audit.add_native_margin_targets(self.frame, self.domains, self.h.base)
        scored = frame[frame.run_name.eq('run')].copy()
        scored['fit_run'] = scored.run_name
        scored = audit.apply_native_outputs(scored, np.full(len(scored), .2))
        candidates = self.h.source_and_candidates()[2]
        rows, veto = audit.candidate_gate_predictions(candidates, scored, self.h.base)
        changed = scored.copy(deep=True)
        changed['quality_label_observed'] = False
        changed['net_cash_difference_per_share'] = -999.
        revised, _ = audit.candidate_gate_predictions(candidates, changed, self.h.base)
        self.assertEqual(rows, revised)
        for status, reason in (('unsupported', 'prediction_unsupported'), ('invalid_prediction', 'prediction_invalid'),
                               ('insufficient_prior_quality_runs', 'model_not_fitted')):
            changed = scored.copy()
            changed['model_status'] = status
            changed['predicted_net_margin_per_share'] = np.nan
            rows, callback = audit.candidate_gate_predictions(candidates, changed, self.h.base)
            self.assertTrue(all(row['reason'] == reason and row['predicted_net_margin_per_share'] is None for row in rows))
            self.assertEqual(set(callback(candidates.iloc[0].to_dict())), {'prediction_status', 'predicted_net_margin_per_share', 'reason'})

    def test_baseline_failure_stops_before_cache_read_or_model(self):
        self.config['baseline_binding']['expected_full_pooled_fingerprint'] = '0' * 64
        with patch.object(audit.prior, 'verified_code_source', return_value={}), patch.object(audit.quality, 'assert_runtime_environment', return_value={}), \
             patch.object(audit.quality, 'load_inputs') as inputs, patch.object(audit, 'fit_predict_once') as fitted:
            with self.assertRaisesRegex(ValueError, 'evaluation_failed'):
                audit.evaluate(self.h.source, self.h.candidate_path, self.root / 'dataset', self.root / 'control', self.config, self.root / 'out')
        inputs.assert_not_called()
        fitted.assert_not_called()
        result = json.loads((self.root / 'out/summary.json').read_text())
        self.assertEqual(result['actual_model_fit_calls'], 0)
        self.assertEqual(result['producer_replay_completions'], 1)
        self.assertFalse(result['comparison_valid'])

    def test_training_byte_binding_params_features_hash_and_budgets_fail_before_any_data(self):
        for field in ('binding_hash', 'features', 'params', 'fit_budget', 'predict_budget', 'capacity', 'model_map'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                cfg = copy.deepcopy(self.config)
                if field == 'binding_hash': cfg['training_binding_sha256'] = '0' * 64
                elif field == 'features': cfg['training_binding']['model_contract']['price_features'] = ['side_sign']
                elif field == 'params': cfg['training_binding']['price_model_parameters']['max_depth'] = 9
                elif field == 'fit_budget': cfg['max_model_fit_calls'] = True
                elif field == 'predict_budget': cfg['max_model_predict_calls'] = 2
                elif field == 'capacity': cfg['capacity_model_fit_allowed'] = True
                else: cfg['margin_policy']['method'] = 'price_wrapper'
                if field in ('features', 'params'):
                    cfg['training_binding_sha256'] = audit.prior.digest(audit.prior.canonical(cfg['training_binding']))
                with patch.object(audit.prior, 'load_frozen_candidates') as candidates, patch.object(audit.quality, 'load_inputs') as inputs:
                    with self.assertRaisesRegex(ValueError, 'evaluation_failed'):
                        audit.evaluate(self.h.source, self.h.candidate_path, Path(tmp), Path(tmp), cfg, Path(tmp) / 'out')
                candidates.assert_not_called()
                inputs.assert_not_called()


if __name__ == '__main__':
    unittest.main()
