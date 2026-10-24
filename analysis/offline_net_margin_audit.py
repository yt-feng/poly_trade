"""One native signed unit-margin learner and a frozen decision-time veto.

The saved original policy must reproduce in full before any model is fitted.
Training uses complete, strictly past cash-domain labels; every original test
identity and candidate survives regardless of its future quote quality.
"""
from __future__ import annotations

import argparse
import copy
import math
import os
import re
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import offline_price_calibration_audit as calibration
import offline_quality_capacity_audit as quality

continuation = calibration.continuation
prior = continuation.prior
BASE_ARM = 'C_scheduled_retry_fullstream'
TREATMENT_ARM = 'C_net_margin_veto'
ARMS = (BASE_ARM, TREATMENT_ARM)
FEATURES = quality.FEATURES
PARAMETERS = quality.PRICE_PARAMETERS
NATIVE_OUTPUT_MAP = 'selected_side_net_cash_difference_per_share_equals_raw_output'
POLICY = {'method': 'native_signed_original_window_net_margin_veto',
          'threshold': 'unchanged_base_protocol_edge_floor',
          'undefined_forecast': 'reject_without_backfill',
          'candidate_universe': 'all_original_frozen_candidates',
          'execution': 'unchanged_original_price_forecast_quantity_and_retry'}
TRAINING_KEYS = frozenset({'cache_bindings', 'expected_all_cohort', 'expected_test_cohort',
    'expected_test_cohort_per_run', 'private_inputs', 'environment', 'price_model_parameters',
    'model_artifact_contract', 'model_contract'})
REQUIRED_CODE_FILES = (calibration.REQUIRED_CODE_FILES | quality.UNCHANGED_FILES |
    {'analysis/offline_quality_capacity_audit.py', 'analysis/offline_net_margin_audit.py'})
SCORE_FIELDS = ['market_id', 'run_name', 'slug', 'side', 'decision_ts', 'market_close_ts',
    'fit_run', 'model_status', 'predicted_net_margin_per_share', 'quality_label_observed',
    'cash_domain_label_available_ts', 'net_cash_difference_per_share']
MEMBER_FIELDS = ['fit_run', 'market_id', 'run_name', 'cash_domain_label_available_ts',
    'net_cash_difference_per_share', 'decision_cutoff_ts']
LIMITS = {'summary.json': 4194304, 'baseline-ledger.json': 2097152, 'treatment-ledger.json': 2097152,
    'baseline-prefix-ledger.json': 2097152, 'treatment-prefix-ledger.json': 2097152,
    'scores.csv': 33554432, 'candidate-gate-predictions.jsonl': 524288,
    'training-plans.jsonl': 1048576, 'training-members.csv': 268435456,
    'net-margin-models.jsonl': 134217728, 'exit-attempts.jsonl': 4194304,
    'quote-witness.jsonl': 16777216, 'source-manifest.json': 2097152,
    'input-receipt.json': 1048576}
OUTPUTS = frozenset(LIMITS)
MAX_AGGREGATE_BYTES = 503316480


def training_config(config: Mapping) -> dict:
    return {**copy.deepcopy(config['training_binding']),
            'base_protocol': config['parent_execution_config']['parent_strategy_config']['base_protocol']}


def validate_config(config: dict) -> dict:
    prior.require(config.get('evaluation_mode') == 'offline_net_margin', 'net_margin_mode_required')
    parent = config.get('parent_execution_config')
    prior.require(isinstance(parent, dict) and prior.digest(prior.canonical(parent)) == config.get('parent_execution_config_sha256'),
                  'frozen_parent_execution_hash_required')
    continuation.validate_config(parent)
    for key in ('source_commit', 'source_repository', 'candidate_binding'):
        prior.require(config.get(key) == parent[key], 'outer_parent_input_binding_mismatch')
    prior.require(prior.valid_hash(config.get('expected_code_commit'), 40), 'current_code_commit_required')
    hashes = config.get('code_file_sha256')
    prior.require(isinstance(hashes, dict) and REQUIRED_CODE_FILES.issubset(hashes)
                  and all(isinstance(k, str) and not Path(k).is_absolute() and '..' not in Path(k).parts
                          and prior.valid_hash(v) for k, v in hashes.items()), 'current_code_hash_bindings_required')
    binding = config.get('training_binding')
    prior.require(isinstance(binding, dict) and set(binding) == TRAINING_KEYS
                  and prior.digest(prior.canonical(binding)) == config.get('training_binding_sha256'), 'exact_training_binding_required')
    prior.require(binding['price_model_parameters'] == PARAMETERS
                  and binding['model_artifact_contract'] == quality.MODEL_ARTIFACT_CONTRACT
                  and binding['model_contract'] == quality.MODEL_CONTRACT, 'inherited_feature_and_native_tree_contract_required')
    for name in ('features_csv_sha256', 'domains_csv_sha256', 'control_predictions_csv_sha256'):
        prior.require(prior.valid_hash(binding.get('cache_bindings', {}).get(name)), 'cache_byte_bindings_required')
    for field in ('expected_all_cohort', 'expected_test_cohort'):
        quality.domains_export._validate_cohort(binding[field])
    base = parent['parent_strategy_config']['base_protocol']
    runs = base['run_order'][base['minimum_training_runs']:]
    per_run = binding['expected_test_cohort_per_run']
    prior.require(isinstance(per_run, dict) and set(per_run) == set(runs), 'complete_test_run_cohort_required')
    for value in per_run.values():
        quality.domains_export._validate_cohort(value)
    private = binding['private_inputs']
    prior.require(isinstance(private, dict) and set(private) == {'dataset', 'control'}, 'two_scoped_inputs_required')
    for record in private.values():
        source = record.get('source', {})
        prior.require(prior.valid_hash(record.get('descriptor_sha256')) and set(source) == {'repository', 'commit', 'path'}
                      and prior.valid_hash(source.get('commit'), 40)
                      and isinstance(source.get('repository'), str)
                      and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', source['repository']) is not None
                      and source['repository'].rsplit('/', 1)[1] not in {'.', '..'}
                      and isinstance(source.get('path'), str)
                      and re.fullmatch(r'research_vault/episodes/[0-9a-f]{64}/part-0000[.]vault', source['path']) is not None, 'scoped_input_descriptor_binding_required')
    env = binding['environment']
    prior.require(isinstance(env, dict)
                  and all(env.get(k) == parent['parent_strategy_config']['environment'][k] for k in ('python_major_minor', 'packages'))
                  and all(env.get(k) is True for k in ('must_assert_effective_get_params_exact_before_fit',
                      'must_assert_runtime_versions_before_model_construction', 'must_record_loaded_histogram_implementation_module_file_hashes')),
                  'one_inherited_runtime_required')
    budgets = {'max_model_fit_calls': len(runs), 'max_model_predict_calls': len(runs),
               'max_producer_replays': 2, 'max_new_cloud_runs': 1}
    prior.require(all(type(config.get(k)) is int and config[k] == v for k, v in budgets.items()), 'exact_model_and_replay_budget_required')
    for key in ('control_model_refit_allowed', 'control_model_predict_allowed', 'capacity_model_fit_allowed',
                'capacity_model_predict_allowed', 'live_orders_enabled', 'new_data_collection_allowed'):
        prior.require(config.get(key) is False, 'offline_one_native_learner_required')
    for key in ('actual_fills', 'verified_canary_fills'):
        prior.require(type(config.get(key)) is int and config[key] == 0, 'zero_actual_fill_contract_required')
    prior.require(config.get('margin_policy') == POLICY, 'single_frozen_margin_policy_required')
    baseline = config.get('baseline_binding')
    prior.require(isinstance(baseline, dict) and set(baseline) == {'expected_full_pooled_fingerprint'}
                  and prior.valid_hash(baseline['expected_full_pooled_fingerprint']), 'full_baseline_fingerprint_required')
    prior.require(len(prior.canonical(config)) <= 262144, 'private_config_budget')
    return config


def add_native_margin_targets(frame: pd.DataFrame, domains: pd.DataFrame, base: Mapping) -> pd.DataFrame:
    """Verify existing quote algebra and its availability, preserving every row."""
    prior.require(list(frame.market_id) == list(domains.market_id), 'native_target_identity_mismatch')
    out = frame.copy(deep=True)
    targets, clocks = [], []
    for (_, row), (_, domain) in zip(frame.iterrows(), domains.iterrows()):
        known = bool(row.quality_label_observed)
        if not known:
            prior.require(all(domain[k] == '' for k in ('entry_fill_price', 'exit_fill_price',
                'entry_cash_cost_per_share', 'exit_net_proceeds_per_share', 'net_cash_difference_per_share',
                'cash_domain_label_available_ts')), 'unknown_margin_must_remain_nullable')
            targets.append(np.nan)
            clocks.append(pd.NaT)
            continue
        entry = prior._time(domain.entry_quote_ts)
        exit_ts = prior._time(domain.exit_quote_ts)
        decision, close = prior._time(row.decision_ts), prior._time(row.market_close_ts)
        entry_target = decision + pd.Timedelta(seconds=base['entry_delay_seconds'] + base['latency_seconds'])
        exit_target = entry + pd.Timedelta(seconds=base['hold_seconds'] + base['latency_seconds'])
        max_delay = pd.Timedelta(seconds=base['max_quote_delay_seconds'])
        clock = prior._time(domain.cash_domain_label_available_ts)
        prior.require(prior._time(domain.entry_target_ts) == entry_target
                      and prior._time(domain.exit_target_ts) == exit_target
                      and entry_target <= entry <= entry_target + max_delay
                      and exit_target <= exit_ts <= exit_target + max_delay
                      and decision < entry < exit_ts < close and clock == exit_ts
                      and clock == prior._time(row.label_available_ts), 'native_target_availability_clock_mismatch')
        p_in = prior._number(domain.entry_ask_price) + base['slippage']
        p_out = prior._number(domain.exit_bid_price) - base['slippage']
        c_in = p_in + base['fee_rate'] * p_in * (1. - p_in)
        c_out = p_out - base['fee_rate'] * p_out * (1. - p_out)
        expected = {'entry_fill_price': p_in, 'exit_fill_price': p_out,
                    'entry_cash_cost_per_share': c_in, 'exit_net_proceeds_per_share': c_out,
                    'net_cash_difference_per_share': c_out - c_in}
        prior.require(all(math.isclose(prior._number(domain[k]), v, rel_tol=0., abs_tol=1e-12)
                          for k, v in expected.items()), 'native_margin_cash_algebra_mismatch')
        targets.append(prior._number(domain.net_cash_difference_per_share))
        clocks.append(clock)
    out['net_cash_difference_per_share'] = targets
    out['cash_domain_label_available_ts'] = pd.to_datetime(clocks, utc=True)
    prior.require(out.net_cash_difference_per_share.notna().equals(out.quality_label_observed)
                  and out.cash_domain_label_available_ts.notna().equals(out.quality_label_observed),
                  'native_target_quality_membership_mismatch')
    return out


def native_training_plans(frame: pd.DataFrame, base: Mapping) -> list[dict]:
    plans = quality.build_fit_plan(frame, base)
    for plan in plans:
        # The inherited helper's capacity constant is not a new learner and is
        # unused. Do not export it or use it to select the native margin model.
        plan['reference'].pop('capacity_constant_log_baseline', None)
        train = frame.loc[plan['train_index']]
        cutoff = plan['reference']['decision_cutoff_ts']
        prior.require(train.quality_label_observed.all()
                      and train.net_cash_difference_per_share.notna().all()
                      and (train.cash_domain_label_available_ts < pd.Timestamp(cutoff)).all(),
                      'native_training_future_or_unknown_label')
        prior.require(train.label_available_ts.equals(train.cash_domain_label_available_ts),
                      'native_training_clock_changed_membership')
    return plans


def apply_native_outputs(current: pd.DataFrame, values: Any, status: str = 'fitted') -> pd.DataFrame:
    raw = quality.histogram.finite_array(values, 'net_margin_prediction')
    prior.require(raw.shape == (len(current),), 'native_prediction_shape_mismatch')
    out = current.copy(deep=True)
    out['model_status'] = status
    out['predicted_net_margin_per_share'] = raw
    return out


def fit_predict_once(train: pd.DataFrame, current: pd.DataFrame, config: Mapping) -> tuple[np.ndarray, dict]:
    """One signed target fit and exactly one estimator.predict per causal fold."""
    fit_calls = predict_calls = 0
    try:
        cfg = training_config(config)
        environment = quality.assert_runtime_environment(cfg)
        train_X = quality.histogram.finite_array(train[FEATURES], 'net_margin_train_features')
        test_X = quality.histogram.finite_array(current[FEATURES], 'net_margin_test_features')
        target = quality.histogram.finite_array(train.net_cash_difference_per_share, 'net_margin_target')
        params = cfg['price_model_parameters']
        prior.require(params == PARAMETERS, 'fixed_native_margin_parameters_required')
        model = HistGradientBoostingRegressor(**params)
        prior.require(model.get_params(deep=False) == params, 'effective_native_parameters_mismatch')
        scaler = StandardScaler()
        transformed = scaler.fit_transform(train_X)
        fit_calls = 1
        model.fit(transformed, target)
        test_transformed = scaler.transform(test_X)
        predict_calls = 1
        raw = quality.histogram.finite_array(model.predict(test_transformed), 'native_margin_output')
        prior.require(raw.shape == (len(current),), 'native_prediction_shape_mismatch')
        # Only the generic numeric tree/scaler serializer is reused. Its price
        # target fitter and side/bid/clipping output adapter are never invoked.
        artifact = quality.serialize_histogram_model(model, scaler, cfg, 'price')
        artifact.update(model_kind='net_margin', output_map=NATIVE_OUTPUT_MAP, runtime_environment=environment)
        reconstructed = quality.reconstruct_serialized_histogram(artifact, test_X)
        prior.require(np.allclose(reconstructed, raw, rtol=1e-12, atol=1e-12), 'native_tree_reconstruction_mismatch')
        artifact['prediction_reconstruction'] = {'rows': len(current), 'rtol': 1e-12, 'atol': 1e-12,
            'maximum_absolute_difference': float(np.max(np.abs(reconstructed - raw))) if len(current) else 0.,
            'estimator_predict_calls': 1, 'tree_only_reconstructions': 1}
        prior.canonical(artifact)
        return raw, artifact
    except Exception as exc:
        exc.audit_fit_calls = fit_calls
        exc.audit_predict_calls = predict_calls
        raise


def candidate_gate_predictions(candidates: pd.DataFrame, scored: pd.DataFrame, base: Mapping) -> tuple[list[dict], Any]:
    prior.require(not scored.market_id.duplicated().any(), 'duplicate_scored_identity')
    by_id = {row.market_id: row for _, row in scored.iterrows()}
    rows, evidence_by_id = [], {}
    for candidate_index, (_, candidate) in enumerate(candidates.iterrows()):
        score = by_id.get(candidate.market_id)
        prior.require(score is not None and all(str(score[k]) == str(candidate[k]) for k in ('run_name', 'slug', 'side'))
                      and prior._time(score.decision_ts) == prior._time(candidate.decision_ts), 'candidate_scored_identity_missing')
        value = score.predicted_net_margin_per_share
        if score.model_status == 'fitted':
            if isinstance(value, (bool, np.bool_)) or not math.isfinite(float(value)):
                evidence = {'prediction_status': 'invalid_prediction', 'predicted_net_margin_per_share': None, 'reason': 'prediction_invalid'}
            else:
                evidence = {'prediction_status': 'available', 'predicted_net_margin_per_share': float(value), 'reason': 'prediction_available'}
        elif score.model_status == 'invalid_prediction':
            prior.require(pd.isna(value), 'invalid_prediction_must_remain_nullable')
            evidence = {'prediction_status': 'invalid_prediction', 'predicted_net_margin_per_share': None, 'reason': 'prediction_invalid'}
        elif score.model_status == 'unsupported':
            prior.require(pd.isna(value), 'unsupported_prediction_must_remain_nullable')
            evidence = {'prediction_status': 'unsupported', 'predicted_net_margin_per_share': None, 'reason': 'prediction_unsupported'}
        else:
            prior.require(pd.isna(value), 'undefined_prediction_must_remain_nullable')
            evidence = {'prediction_status': 'not_fitted', 'predicted_net_margin_per_share': None, 'reason': 'model_not_fitted'}
        evidence_by_id[candidate.market_id] = evidence
        rows.append({'candidate_index': candidate_index, **{k: str(candidate[k]) for k in ('market_id', 'run_name', 'slug', 'side')},
            'decision_ts': prior._time(candidate.decision_ts).isoformat(), 'fit_run': str(score.fit_run),
            'model_status': str(score.model_status), 'edge_floor': base['edge_floor'], **evidence,
            'passes_margin_veto': evidence['prediction_status'] == 'available' and evidence['predicted_net_margin_per_share'] >= base['edge_floor']})
    frozen_candidates = {str(row.market_id): {k: row[k] for k in prior.CANDIDATE_FIELDS} for _, row in candidates.iterrows()}
    def decision_veto(causal_candidate):
        mid = causal_candidate['market_id']
        prior.require(mid in evidence_by_id and set(causal_candidate) == set(prior.CANDIDATE_FIELDS), 'veto_causal_candidate_binding')
        expected = frozen_candidates[mid]
        prior.require(all((prior._time(causal_candidate[k]) == prior._time(expected[k]) if k in ('decision_ts', 'market_close_ts')
                          else causal_candidate[k] == expected[k]) for k in prior.CANDIDATE_FIELDS), 'veto_original_candidate_changed')
        return dict(evidence_by_id[mid])
    return rows, decision_veto


def write_bytes(path: Path, raw: bytes, *, append: bool = False) -> None:
    prior.require(path.name in OUTPUTS and not path.is_symlink(), 'result_member_name_or_link')
    previous = path.stat().st_size if append and path.exists() else 0
    prior.require(previous + len(raw) <= LIMITS[path.name], 'result_member_size_limit')
    prior.require(sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file()) + len(raw) <= MAX_AGGREGATE_BYTES,
                  'result_aggregate_size_limit')
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'ab' if append else 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def write_artifact(path: Path, value: Any, *, jsonl: bool = False, append: bool = False) -> None:
    raw = b''.join(prior.canonical(prior.json_safe(row)) + b'\n' for row in value) if jsonl else prior.canonical(prior.json_safe(value)) + b'\n'
    write_bytes(path, raw, append=append)


def write_csv(path: Path, frame: pd.DataFrame, fields: list[str], *, append: bool = False) -> None:
    text = frame[fields].to_csv(index=False, header=not (append and path.exists()), lineterminator='\n', float_format='%.17g')
    write_bytes(path, text.encode(), append=append)


def margin_metrics(scored: pd.DataFrame) -> dict:
    paired = scored.quality_label_observed & scored.predicted_net_margin_per_share.notna()
    errors = (scored.loc[paired, 'predicted_net_margin_per_share'] - scored.loc[paired, 'net_cash_difference_per_share']).to_numpy(dtype=float)
    return {'all_test_rows': len(scored), 'known_quality_targets': int(scored.quality_label_observed.sum()),
        'unknown_quality_targets_retained': int((~scored.quality_label_observed).sum()),
        'forecast_rows': int(scored.predicted_net_margin_per_share.notna().sum()), 'paired_rows': int(paired.sum()),
        'mae': float(np.mean(np.abs(errors))) if len(errors) else None,
        'mean_error': float(np.mean(errors)) if len(errors) else None,
        'basis': 'Signed original-window quoted net margin on the unchanged historical cohort; not retry completion, actual cash or untouched forward evidence.'}


def evaluate(source_root: Path, candidate_path: Path, dataset_root: Path, control_root: Path, config: dict, output: Path) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    prior.require(not output.is_symlink() and not any(output.iterdir()), 'empty_private_output_required')
    output.chmod(0o700)
    summary = {'format': 'offline-net-margin-audit-v1', 'evaluation_status': 'failed', 'strategy_evaluated': False,
        'comparison_valid': False, 'baseline_control_reproduced': False, 'prefix_control_reproduced': False,
        'control_execution_reproduced': False, 'input_verified': False, 'runtime_environment': None,
        'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0, 'net_margin_model_fit_calls': 0,
        'net_margin_model_predict_calls': 0, 'control_model_fit_calls': 0, 'control_model_predict_calls': 0,
        'capacity_model_fit_calls': 0, 'capacity_model_predict_calls': 0,
        'model_scored_rows': 0, 'model_forecast_rows': 0, 'gate_rows': 0, 'model_tree_reconstructions': 0, 'replayed_cells': 0,
        'producer_replayed_cells': 0, 'producer_replay_completions': 0, 'candidate_rows': None,
        'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False,
        'new_data_collected': False, 'new_policy_hypotheses': 1, 'fit_records': [],
        'reconstruction_counters': {'model': 0, 'producer_ledger_replays': 0,
            'independent_audit_reconstructions': None, 'execution_evidence_original_projections': 0}}
    sources, collectors = [], {}
    stage, failure = 'configuration', None
    try:
        validate_config(config)
        parent = config['parent_execution_config']
        strategy = parent['parent_strategy_config']
        base = strategy['base_protocol']
        summary.update(protocol=config, protocol_sha256=prior.digest(prior.canonical(config)))
        stage = 'runtime_environment'
        summary['runtime_environment'] = prior.probe_runtime_environment()
        prior.assert_runtime_environment(strategy, summary['runtime_environment'])
        quality.assert_runtime_environment(training_config(config))
        stage = 'immutable_inputs'
        summary.update(prior.verified_code_source(Path(source_root), config))
        candidates = prior.load_frozen_candidates(Path(candidate_path), strategy['candidate_binding'], strategy['frozen_control_candidates'])
        calibration.ordered_candidates(candidates)
        summary['candidate_rows'] = len(candidates)
        summary['candidate_clock_coverage'] = {'first_decision_ts': candidates.decision_ts.min().isoformat(),
            'last_decision_ts': candidates.decision_ts.max().isoformat(),
            'primary_runs': base['run_order'][base['minimum_training_runs']:],
            'candidate_runs': list(dict.fromkeys(candidates.run_name)), 'continuous_five_minute_coverage_inferred': False}
        loaded, manifest = prior.load_candidate_quote_books(Path(source_root), candidates, base, strategy['raw_budget'],
            deadline_after_original_target_seconds=strategy['retry_policy']['deadline_after_original_target_seconds'])
        write_artifact(output / 'source-manifest.json', manifest)
        baseline_source = calibration.PhaseQuoteSource(loaded, BASE_ARM, sources)
        treatment_source = calibration.PhaseQuoteSource(loaded, TREATMENT_ARM, sources)
        from scheduled_exit_retry import RetryPolicy, ScheduledExitResolver
        def replay(source, arm, required, veto=None):
            captured, calls = None, 0
            def observe(raw_prefix):
                nonlocal captured, calls
                calls += 1
                prior.require(calls == 1, 'duplicate_prefix_snapshot')
                captured = prior.pooled_wrapper(raw_prefix, base)
                if required:
                    frozen = parent['prefix_binding']
                    actual = prior.fingerprints.result_fingerprint(captured)
                    summary['prefix_full_fingerprint'] = actual
                    prior.require(actual == frozen['expected_pooled_fingerprint']
                                  and prior.fingerprints.normalized_result(captured) == prior.fingerprints.normalized_result(frozen['frozen_pooled_result']),
                                  'required_baseline_prefix_mismatch')
                    summary['prefix_control_reproduced'] = True
                    summary['prefix_guard_selector_request_count'] = len(source.requests)
                    summary['prefix_guard_processed_candidate_count'] = len(raw_prefix['chain']) + len(raw_prefix['attempts'])
            resolver = continuation.ContinuationEvidenceCollector(ScheduledExitResolver(RetryPolicy(**strategy['retry_policy'])),
                len(candidates) * (1 + strategy['retry_policy']['max_extra_attempts']))
            collectors[arm] = resolver
            prior.require(summary['producer_replayed_cells'] < config['max_producer_replays'], 'producer_replay_budget')
            summary['replayed_cells'] += 1
            summary['producer_replayed_cells'] += 1
            summary['reconstruction_counters']['producer_ledger_replays'] += 1
            kwargs = prior.replay_kwargs(base)
            if veto is not None:
                kwargs['decision_veto'] = veto
            raw = prior.execution.replay_cached_quotes(candidates, base['run_order'][base['minimum_training_runs']:], source,
                exit_resolver=resolver, continue_after_required_fills=True, required_prefix_observer=observe, **kwargs)
            summary['producer_replay_completions'] += 1
            full = prior.pooled_wrapper(raw, base)
            if required:
                prior.require(calls == 1 and captured is not None and raw.get('required_prefix_result') is not None,
                              'required_baseline_prefix_not_reproduced')
            prior.require((captured is None) == (raw.get('required_prefix_result') is None), 'prefix_snapshot_presence_mismatch')
            if captured is not None:
                prior.require(prior.fingerprints.normalized_result(prior.pooled_wrapper(raw['required_prefix_result'], base))
                              == prior.fingerprints.normalized_result(captured), 'returned_prefix_snapshot_mismatch')
            prior.require(raw['processed_candidate_count'] == len(raw['chain']) + len(raw['attempts'])
                          and raw['candidate_stream_count'] == len(candidates)
                          and raw['candidate_stream_exhausted'] == (raw['processed_candidate_count'] == len(candidates)),
                          'candidate_count_conservation')
            return full, captured
        stage = 'baseline_fullstream'
        baseline, baseline_prefix = replay(baseline_source, BASE_ARM, True)
        write_artifact(output / 'baseline-ledger.json', baseline)
        write_artifact(output / 'baseline-prefix-ledger.json', baseline_prefix)
        baseline_fp = prior.fingerprints.result_fingerprint(baseline)
        summary['baseline_full_fingerprint'] = baseline_fp
        prior.require(baseline_fp == config['baseline_binding']['expected_full_pooled_fingerprint'], 'full_baseline_fingerprint_mismatch')
        summary['baseline_control_reproduced'] = summary['control_execution_reproduced'] = True
        stage = 'scoped_cache_and_native_target_contract'
        cfg = training_config(config)
        frame, domains, control = quality.load_inputs(Path(dataset_root), Path(control_root), cfg)
        frame = add_native_margin_targets(frame, domains, base)
        tests = base['run_order'][base['minimum_training_runs']:]
        cohort = frame[frame.run_name.isin(tests)].copy()
        plans = native_training_plans(frame, base)
        summary['all_test_rows'] = len(cohort)
        summary['all_test_cohort'] = quality.cohort_contract(cohort)
        write_artifact(output / 'input-receipt.json', {'format': 'net-margin-input-receipt-v1', 'status': 'complete',
            'candidate_binding': strategy['candidate_binding'], 'candidate_bytes_verified': True,
            'source_byte_hashes_verified': True, 'cache_bindings': cfg['cache_bindings'],
            'private_inputs': cfg['private_inputs'], 'all_cohort': quality.cohort_contract(frame),
            'test_cohort': quality.cohort_contract(cohort), 'causal_fields': list(prior.CANDIDATE_FIELDS),
            'future_fields_excluded_from_model_features': True, 'model_features': FEATURES,
            'base_protocol_sha256': strategy['base_protocol_sha256'],
            'parent_execution_config_sha256': config['parent_execution_config_sha256'],
            'source_manifest_sha256': prior.digest(prior.canonical(manifest) + b'\n')})
        summary['input_verified'] = True
        parts = []
        for plan in plans:
            reference = plan['reference']
            record = {**reference, 'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0}
            summary['fit_records'].append(record)
            current = frame.loc[plan['test_index']].copy()
            current['fit_run'] = reference['run_name']
            if not plan['eligible']:
                current['model_status'] = reference['status']
                current['predicted_net_margin_per_share'] = np.nan
            else:
                train = frame.loc[plan['train_index']].copy()
                member = train[['market_id', 'run_name', 'cash_domain_label_available_ts', 'net_cash_difference_per_share']].copy()
                member.insert(0, 'fit_run', reference['run_name'])
                member['decision_cutoff_ts'] = pd.Timestamp(reference['decision_cutoff_ts'])
                write_csv(output / 'training-members.csv', member, MEMBER_FIELDS, append=True)
                stage = 'native_margin_model_fitting'
                prior.require(summary['actual_model_fit_calls'] < config['max_model_fit_calls']
                              and summary['actual_model_predict_calls'] < config['max_model_predict_calls'], 'model_computation_budget')
                try:
                    values, artifact = fit_predict_once(train, current, config)
                except Exception as exc:
                    fc, pc = int(getattr(exc, 'audit_fit_calls', 0)), int(getattr(exc, 'audit_predict_calls', 0))
                    summary['actual_model_fit_calls'] += fc
                    summary['actual_model_predict_calls'] += pc
                    summary['net_margin_model_fit_calls'] += fc
                    summary['net_margin_model_predict_calls'] += pc
                    record.update(actual_model_fit_calls=fc, actual_model_predict_calls=pc, status='model_call_failed')
                    write_artifact(output / 'training-plans.jsonl', [record], jsonl=True, append=True)
                    raise
                for name in ('actual_model_fit_calls', 'actual_model_predict_calls', 'net_margin_model_fit_calls',
                             'net_margin_model_predict_calls', 'model_tree_reconstructions'):
                    summary[name] += 1
                summary['reconstruction_counters']['model'] += 1
                record.update(actual_model_fit_calls=1, actual_model_predict_calls=1)
                write_artifact(output / 'net-margin-models.jsonl', [{'fit_run': reference['run_name'],
                    'cutoff_ts': reference['decision_cutoff_ts'], 'training_rows': len(train),
                    'training_known_runs': reference['training_known_runs'],
                    'training_market_ids_sha256': reference['training_market_ids_sha256'], **artifact}], jsonl=True, append=True)
                current = apply_native_outputs(current, values)
                summary['model_forecast_rows'] += len(current)
            parts.append(current)
            write_csv(output / 'scores.csv', current, SCORE_FIELDS, append=True)
            write_artifact(output / 'training-plans.jsonl', [record], jsonl=True, append=True)
        for name, fields in (('training-members.csv', MEMBER_FIELDS), ('scores.csv', SCORE_FIELDS)):
            if not (output / name).exists():
                write_csv(output / name, pd.DataFrame(columns=fields), fields)
        for name in ('training-plans.jsonl', 'net-margin-models.jsonl'):
            if not (output / name).exists():
                write_artifact(output / name, [], jsonl=True)
        scored = pd.concat(parts, ignore_index=True) if parts else cohort.iloc[:0].copy()
        prior.require(quality.cohort_contract(scored) == cfg['expected_test_cohort']
                      and list(scored.market_id) == list(cohort.market_id), 'full_test_cohort_must_remain')
        summary['model_scored_rows'] = len(scored)
        stage = 'candidate_margin_veto_binding'
        gate_rows, veto = candidate_gate_predictions(candidates, scored, base)
        write_artifact(output / 'candidate-gate-predictions.jsonl', gate_rows, jsonl=True)
        summary['gate_rows'] = len(gate_rows)
        summary['candidate_gate_predictions'] = {'all_candidates': len(gate_rows),
            'available': sum(r['prediction_status'] == 'available' for r in gate_rows),
            'pass': sum(r['passes_margin_veto'] for r in gate_rows),
            'reject': sum(not r['passes_margin_veto'] for r in gate_rows), 'edge_floor': base['edge_floor']}
        summary['native_margin_metrics'] = margin_metrics(scored)
        stage = 'treatment_fullstream'
        treatment, treatment_prefix = replay(treatment_source, TREATMENT_ARM, False, veto)
        write_artifact(output / 'treatment-ledger.json', treatment)
        write_artifact(output / 'treatment-prefix-ledger.json', treatment_prefix)
        summary.update(arms={BASE_ARM: baseline, TREATMENT_ARM: treatment},
            prefix_ledgers={BASE_ARM: baseline_prefix, TREATMENT_ARM: treatment_prefix},
            arm_endpoints={BASE_ARM: calibration.arm_endpoints(baseline, baseline_prefix, base),
                           TREATMENT_ARM: calibration.arm_endpoints(treatment, treatment_prefix, base)},
            evaluation_status='evaluated', strategy_evaluated=True, comparison_valid=True)
    except Exception as exc:
        failure = exc
        summary['failure'] = {'stage': stage, 'type': type(exc).__name__, 'code': str(exc)}
    finally:
        attempts = []
        for arm in ARMS:
            collector = collectors.get(arm)
            if collector is not None:
                attempts.extend({'arm': arm, **row} for row in collector.attempt_evidence)
                summary['reconstruction_counters']['execution_evidence_original_projections'] += collector.original_projection_calls
        artifact_failures = []
        def retain(name, value):
            nonlocal failure
            try:
                if name == 'exit-attempts.jsonl':
                    prior.require(len(value) <= 300, 'combined_exit_attempt_budget')
                write_artifact(output / name, value, jsonl=True)
            except Exception as exc:
                artifact_failures.append({'member': name, 'type': type(exc).__name__, 'code': str(exc)})
                if failure is None:
                    failure = exc
                    summary['failure'] = {'stage': 'evidence_retention', 'type': type(exc).__name__, 'code': str(exc)}
                summary.update(evaluation_status='failed', strategy_evaluated=False, comparison_valid=False)
        retain('exit-attempts.jsonl', attempts)
        if sources:
            summary['quote_requests'] = {source.phase: source.requests for source in sources}
            summary['quote_request_counts'] = {source.phase: {'requests': len(source.requests),
                'by_saved_selector_status': {status: sum(r['quote_status'] == status for r in source.requests) for status in calibration.STATUSES}}
                for source in sources}
            retain('quote-witness.jsonl', [row for source in sources for row in source.witness])
        if artifact_failures:
            summary['artifact_failures'] = artifact_failures
        write_artifact(output / 'summary.json', summary)
    if failure is not None:
        raise ValueError('offline_net_margin_evaluation_failed') from None
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-root', 'candidate-path', 'dataset-root', 'control-root', 'config', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    config = prior.load_json(prior.read_bounded(Path(args.config), 262144))
    evaluate(Path(args.source_root), Path(args.candidate_path), Path(args.dataset_root), Path(args.control_root), config, Path(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
