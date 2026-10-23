"""One frozen baseline and one strictly-past expanding residual correction.

Reference labels concern every frozen candidate's original quote window and are
independent of either trading ledger.  Only the treatment's saved forecast field
changes; execution, quantities, fees, retry policy and cash accounting are shared.
"""
from __future__ import annotations
import argparse
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import offline_exit_continuation_audit as continuation

prior = continuation.prior
BASE_ARM = 'C_scheduled_retry_fullstream'
TREATMENT_ARM = 'C_price_residual_calibration'
REFERENCE_ARM = 'reference_labels'
PHASES = (BASE_ARM, REFERENCE_ARM, TREATMENT_ARM)
ARMS = (BASE_ARM, TREATMENT_ARM)
POLICY = {'method': 'strict_past_expanding_positive_mean',
          'label_availability': 'selected_original_exit_quote',
          'missing_label': 'exclude', 'history_universe': 'all_frozen_candidates'}
REQUIRED_CODE_FILES = continuation.REQUIRED_CODE_FILES | {'analysis/offline_price_calibration_audit.py'}
LIMITS = {'summary.json': 2097152, 'baseline-ledger.json': 1048576, 'treatment-ledger.json': 1048576,
          'baseline-prefix-ledger.json': 1048576, 'treatment-prefix-ledger.json': 1048576,
          'reference-labels.jsonl': 262144, 'calibration-trace.jsonl': 524288,
          'exit-attempts.jsonl': 2097152, 'quote-witness.jsonl': 16777216,
          'source-manifest.json': 2097152, 'input-receipt.json': 262144}
OUTPUTS = frozenset(LIMITS)
STATUSES = ('selected', 'target_at_or_after_close', 'missing_book', 'no_quote_in_window',
            'unknown_quote_conflict', 'invalid_quote_price')


def validate_config(config: dict) -> dict:
    prior.require(config.get('evaluation_mode') == 'offline_price_calibration', 'price_calibration_mode_required')
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
    prior.require(type(config.get('max_producer_replays')) is int and config['max_producer_replays'] == 2
                  and type(config.get('max_calibrated_forecast_transforms')) is int and config['max_calibrated_forecast_transforms'] == 50
                  and type(config.get('actual_model_fit_calls')) is int and config['actual_model_fit_calls'] == 0
                  and type(config.get('actual_model_predict_calls')) is int and config['actual_model_predict_calls'] == 0
                  and config.get('live_orders_enabled') is False, 'two_model_free_calibration_ledgers_required')
    prior.require(config.get('calibration_policy') == POLICY, 'single_frozen_calibration_policy_required')
    binding = config.get('baseline_binding')
    prior.require(isinstance(binding, dict) and set(binding) == {'expected_full_pooled_fingerprint'}
                  and prior.valid_hash(binding['expected_full_pooled_fingerprint']), 'full_baseline_fingerprint_required')
    return config


def ordered_candidates(candidates):
    prior.require(tuple(candidates.columns) == prior.CANDIDATE_FIELDS and 0 < len(candidates) <= 50,
                  'causal_candidate_projection_required')
    prior.require(not candidates['market_id'].duplicated().any() and not candidates['slug'].duplicated().any(),
                  'candidate_identity_duplicate')
    frame = candidates.copy()
    frame['decision_ts'] = frame['decision_ts'].map(prior._time)
    frame['market_close_ts'] = frame['market_close_ts'].map(prior._time)
    frame['_candidate_index'] = range(len(frame))
    return frame.sort_values(['decision_ts', 'slug', 'side'], kind='mergesort')


class PhaseQuoteSource(prior.RawQuoteSource):
    """Independent request streams over a single immutable normalized book load."""
    def __init__(self, loaded, phase: str, peers: list):
        super().__init__(loaded.books, loaded.files, loaded.conflicts)
        self.phase = phase
        self.peers = peers
        peers.append(self)

    def select(self, *args, **kwargs):
        selected = super().select(*args, **kwargs)
        prior.require(sum(p.witness_bytes for p in self.peers) <= LIMITS['quote-witness.jsonl']
                      and sum(len(p.witness) for p in self.peers) <= 100000,
                      'combined_quote_witness_budget')
        return selected


def extract_reference_labels(candidates, source, base: Mapping) -> list[dict]:
    """Observe original targets only, without any ledger entry/cost/depth gate."""
    ordered = ordered_candidates(candidates)
    labels = []
    for execution_index, (_, row) in enumerate(ordered.iterrows()):
        decision, close = prior._time(row['decision_ts']), prior._time(row['market_close_ts'])
        target = decision + prior.pd.Timedelta(seconds=base['entry_delay_seconds'] + base['latency_seconds'])
        entry, entry_status = source.select(row['market_id'], target, close, side=row['side'], kind='entry',
                                            max_quote_delay_seconds=base['max_quote_delay_seconds'])
        record = {'candidate_index': int(row['_candidate_index']), 'execution_index': execution_index,
                  **{k: str(row[k]) for k in ('market_id', 'run_name', 'slug', 'side')},
                  'decision_ts': decision.isoformat(), 'market_close_ts': close.isoformat(),
                  'original_predicted_exit_bid_price': prior._number(row['predicted_exit_bid_price']),
                  'target_entry_arrival_ts': target.isoformat(), 'target_entry_quote_ts': None,
                  'target_entry_quote_status': entry_status, 'target_exit_arrival_ts': None,
                  'target_exit_quote_ts': None, 'target_exit_quote_status': None,
                  'future_bid_price': None, 'label_available_ts': None,
                  'target_status': 'entry_' + entry_status, 'original_price_residual': None}
        prior.require((entry is None) == (entry_status != 'selected'), 'reference_entry_selector_contract')
        if entry is not None:
            entry_ts = prior._time(entry['ts_utc'])
            exit_target = entry_ts + prior.pd.Timedelta(seconds=base['hold_seconds'] + base['latency_seconds'])
            quote, status = source.select(row['market_id'], exit_target, close, side=row['side'], kind='exit',
                                         max_quote_delay_seconds=base['max_quote_delay_seconds'])
            record.update(target_entry_quote_ts=entry_ts.isoformat(), target_exit_arrival_ts=exit_target.isoformat(),
                          target_exit_quote_status=status, target_status='exit_' + status)
            prior.require((quote is None) == (status != 'selected'), 'reference_exit_selector_contract')
            if quote is not None:
                stamp = prior._time(quote['ts_utc'])
                bid = prior._number(quote['sell_' + row['side'] + '_cents']) / 100.
                prior.require(0 < bid < 1, 'selected_reference_bid_domain')
                record.update(target_exit_quote_ts=stamp.isoformat(), future_bid_price=bid,
                              label_available_ts=stamp.isoformat(), target_status='known',
                              original_price_residual=record['original_predicted_exit_bid_price'] - bid)
        labels.append(record)
    return labels


def calibrate_candidates(candidates, labels: list[dict]):
    """Compute all decision-frozen transforms, including candidates never traded."""
    ordered = ordered_candidates(candidates)
    prior.require(isinstance(labels, list) and len(labels) == len(ordered), 'one_reference_per_candidate_required')
    by_id = {}
    for row in labels:
        mid = row.get('market_id')
        prior.require(isinstance(mid, str) and mid not in by_id, 'reference_identity_duplicate')
        by_id[mid] = row
    prior.require(set(by_id) == set(ordered['market_id']), 'reference_candidate_identity_mismatch')
    for _, row in ordered.iterrows():
        label = by_id[row['market_id']]
        prior.require(all(label.get(k) == str(row[k]) for k in ('run_name', 'slug', 'side'))
                      and prior._time(label['decision_ts']) == row['decision_ts']
                      and prior._time(label['market_close_ts']) == row['market_close_ts']
                      and prior._number(label['original_predicted_exit_bid_price']) == prior._number(row['predicted_exit_bid_price']),
                      'reference_candidate_binding_mismatch')
        if label['target_status'] == 'known':
            bid = prior._number(label['future_bid_price'])
            error = prior._number(label['original_price_residual'])
            prior.require(0 < bid < 1 and error == prior._number(row['predicted_exit_bid_price']) - bid
                          and prior._time(label['label_available_ts']) == prior._time(label['target_exit_quote_ts'])
                          and prior._time(label['label_available_ts']) > row['decision_ts'], 'known_reference_label_contract')
        else:
            prior.require(label['target_status'] in {'entry_' + status for status in STATUSES if status != 'selected'}
                          | {'exit_' + status for status in STATUSES if status != 'selected'}, 'unknown_reference_status_invalid')
            prior.require(label.get('future_bid_price') is None and label.get('label_available_ts') is None
                          and label.get('original_price_residual') is None, 'unknown_reference_must_not_fill_zero')
    transformed = candidates.copy(deep=True)
    trace = []
    history = []
    for execution_index, (_, row) in enumerate(ordered.iterrows()):
        decision = row['decision_ts']
        used = [label for label in history if label['market_id'] != row['market_id']
                and prior._time(label['decision_ts']) < decision
                and label['target_status'] == 'known'
                and prior._time(label['label_available_ts']) < decision]
        errors = [prior._number(label['original_price_residual']) for label in used]
        total = math.fsum(errors)
        mean = total / len(errors) if errors else 0.
        delta = max(0., mean)
        original = prior._number(row['predicted_exit_bid_price'])
        calibrated = max(0., original - delta)
        candidate_index = int(row['_candidate_index'])
        transformed.iloc[candidate_index, transformed.columns.get_loc('predicted_exit_bid_price')] = calibrated
        clocks = [label['label_available_ts'] for label in used]
        maximum = max(clocks, key=prior._time) if clocks else None
        prior.require(maximum is None or prior._time(maximum) < decision, 'calibration_future_label_leak')
        trace.append({'candidate_index': candidate_index, 'execution_index': execution_index,
                      **{k: str(row[k]) for k in ('market_id', 'run_name', 'slug', 'side')},
                      'decision_ts': decision.isoformat(), 'original_predicted_exit_bid_price': original,
                      'used_market_ids': [label['market_id'] for label in used], 'used_reference_errors': errors,
                      'used_label_available_ts': clocks, 'max_used_label_ts': maximum, 'reference_count': len(used),
                      'error_fsum': total, 'mean_past_error': mean, 'delta': delta,
                      'calibrated_predicted_exit_bid_price': calibrated})
        history.append(by_id[row['market_id']])
    prior.require(list(transformed.columns) == list(candidates.columns)
                  and transformed.drop(columns='predicted_exit_bid_price').equals(candidates.drop(columns='predicted_exit_bid_price')),
                  'only_forecast_may_change')
    return transformed, trace


def write_artifact(path: Path, value: Any, *, jsonl: bool = False) -> None:
    raw = b''.join(prior.canonical(prior.json_safe(row)) + b'\n' for row in value) if jsonl else prior.canonical(prior.json_safe(value)) + b'\n'
    prior.require(path.name in OUTPUTS and len(raw) <= LIMITS[path.name], 'result_member_size_or_name_limit')
    prior.require(sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file()) + len(raw) <= 33554432,
                  'result_aggregate_size_limit')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def optional_prefix_readouts(full: Mapping, prefix: Mapping | None, base: Mapping) -> dict:
    if prefix is not None:
        return continuation.cash_time_readouts(full, prefix, base)
    positions = prior.cash_time_readout(full, base)['positions']
    complete = bool(full['chain']) and full['full_sequence_resolved']
    first = positions[0]['entry_ts'] if positions else None
    last = positions[-1] if positions else None
    release = last['assumed_cash_release_ts'] if last else None
    elapsed = (prior._time(release).value - prior._time(first).value) / 1e9 if complete and first and release else None
    return {'prefix': None, 'full': {'positions': positions, 'fully_resolved': complete, 'first_entry_ts': first,
            'last_exit_ts': last['exit_ts'] if last else None, 'last_assumed_cash_release_ts': release,
            'elapsed_first_entry_to_final_assumed_release_seconds': elapsed,
            'elapsed_status': 'complete_assumed_cash_clock' if elapsed is not None else 'incomplete_or_unknown',
            'ending_liquid_cash': full['ending_cash'], 'locked_shares': full['locked_shares'],
            'basis': 'One original saved-signal cash path and inherited assumed release clock; no reset or actual settlement proof.'}, 'tail': None}


def arm_endpoints(full: Mapping, prefix: Mapping | None, base: Mapping) -> dict:
    return {'prefix_economic_pass': False if prefix is None else bool(prefix['strict_net_growth_ten']),
            'full_economic_pass': bool(full['full_sequence_resolved'] and full['resolved_fills'] >= base['required_fills']
                                      and full['ending_cash'] > base['initial_cash']),
            'full_net_growth': bool(full['full_sequence_net_growth']) if full['full_sequence_resolved'] else None,
            'cash_time_readouts': optional_prefix_readouts(full, prefix, base)}


def evaluate(source_root: Path, candidate_path: Path, config: dict, output: Path) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    prior.require(not output.is_symlink() and not any(output.iterdir()), 'empty_private_output_required')
    output.chmod(0o700)
    summary = {'format': 'offline-price-calibration-audit-v1', 'evaluation_status': 'failed',
               'strategy_evaluated': False, 'comparison_valid': False, 'baseline_control_reproduced': False,
               'prefix_control_reproduced': False, 'control_execution_reproduced': False, 'input_verified': False,
               'runtime_environment': None, 'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0,
               'replayed_cells': 0, 'producer_replayed_cells': 0, 'producer_replay_completions': 0,
               'calibration_forecast_transforms': 0, 'reference_label_updates': 0, 'candidate_rows': None,
               'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False, 'new_data_collected': False,
               'new_policy_hypotheses': 1,
               'reconstruction_counters': {'model': 0, 'producer_ledger_replays': 0,
                   'independent_audit_reconstructions': None, 'execution_evidence_original_projections': 0}}
    sources = []
    collectors = {}
    stage = 'configuration'
    failure = None
    try:
        validate_config(config)
        parent = config['parent_execution_config']
        strategy = parent['parent_strategy_config']
        base = strategy['base_protocol']
        summary.update(protocol=config, protocol_sha256=prior.digest(prior.canonical(config)))
        stage = 'runtime_environment'
        summary['runtime_environment'] = prior.probe_runtime_environment()
        prior.assert_runtime_environment(strategy, summary['runtime_environment'])
        stage = 'immutable_inputs'
        summary.update(prior.verified_code_source(Path(source_root), config))
        candidates = prior.load_frozen_candidates(Path(candidate_path), strategy['candidate_binding'], strategy['frozen_control_candidates'])
        ordered_candidates(candidates)
        summary['candidate_rows'] = len(candidates)
        summary['candidate_clock_coverage'] = {'first_decision_ts': candidates['decision_ts'].min().isoformat(),
            'last_decision_ts': candidates['decision_ts'].max().isoformat(), 'primary_runs': base['run_order'][base['minimum_training_runs']:],
            'candidate_runs': list(dict.fromkeys(candidates['run_name'])), 'continuous_five_minute_coverage_inferred': False}
        loaded, manifest = prior.load_candidate_quote_books(Path(source_root), candidates, base, strategy['raw_budget'],
            deadline_after_original_target_seconds=strategy['retry_policy']['deadline_after_original_target_seconds'])
        write_artifact(output / 'source-manifest.json', manifest)
        write_artifact(output / 'input-receipt.json', {'format': 'price-calibration-input-receipt-v1', 'status': 'complete',
            'candidate_binding': strategy['candidate_binding'], 'candidate_bytes_verified': True, 'source_byte_hashes_verified': True,
            'causal_fields': list(prior.CANDIDATE_FIELDS), 'future_fields_excluded': True,
            'base_protocol_sha256': strategy['base_protocol_sha256'],
            'parent_execution_config_sha256': config['parent_execution_config_sha256'],
            'source_manifest_sha256': prior.digest(prior.canonical(manifest) + b'\n')})
        summary['input_verified'] = True
        baseline_source = PhaseQuoteSource(loaded, BASE_ARM, sources)
        reference_source = PhaseQuoteSource(loaded, REFERENCE_ARM, sources)
        treatment_source = PhaseQuoteSource(loaded, TREATMENT_ARM, sources)
        from scheduled_exit_retry import RetryPolicy, ScheduledExitResolver
        def replay(frame, source, arm, required):
            captured = None
            calls = 0
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
                len(frame) * (1 + strategy['retry_policy']['max_extra_attempts']))
            collectors[arm] = resolver
            summary['replayed_cells'] += 1
            summary['producer_replayed_cells'] += 1
            summary['reconstruction_counters']['producer_ledger_replays'] += 1
            raw = prior.execution.replay_cached_quotes(frame, base['run_order'][base['minimum_training_runs']:], source,
                exit_resolver=resolver, continue_after_required_fills=True, required_prefix_observer=observe, **prior.replay_kwargs(base))
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
                          and raw['candidate_stream_count'] == len(frame)
                          and raw['candidate_stream_exhausted'] == (raw['processed_candidate_count'] == len(frame)), 'candidate_count_conservation')
            return full, captured
        stage = 'baseline_fullstream'
        baseline, baseline_prefix = replay(candidates, baseline_source, BASE_ARM, True)
        write_artifact(output / 'baseline-ledger.json', baseline)
        write_artifact(output / 'baseline-prefix-ledger.json', baseline_prefix)
        baseline_fp = prior.fingerprints.result_fingerprint(baseline)
        summary['baseline_full_fingerprint'] = baseline_fp
        prior.require(baseline_fp == config['baseline_binding']['expected_full_pooled_fingerprint'], 'full_baseline_fingerprint_mismatch')
        summary['baseline_control_reproduced'] = summary['control_execution_reproduced'] = True
        stage = 'reference_labels'
        labels = extract_reference_labels(candidates, reference_source, base)
        write_artifact(output / 'reference-labels.jsonl', labels, jsonl=True)
        summary['reference_label_updates'] = sum(label['target_status'] == 'known' for label in labels)
        summary['reference_label_coverage'] = {'all_candidates': len(labels), 'known': summary['reference_label_updates'],
            'unknown': len(labels) - summary['reference_label_updates'], 'history_universe': POLICY['history_universe']}
        stage = 'calibration_transforms'
        treatment_candidates, trace = calibrate_candidates(candidates, labels)
        prior.require(len(trace) <= config['max_calibrated_forecast_transforms'], 'calibrated_forecast_transform_budget')
        summary['calibration_forecast_transforms'] = len(trace)
        write_artifact(output / 'calibration-trace.jsonl', trace, jsonl=True)
        known_pairs = [(label, trace_row) for label, trace_row in zip(labels, trace) if label['target_status'] == 'known']
        original_errors = [label['original_price_residual'] for label, _ in known_pairs]
        calibrated_errors = [row['calibrated_predicted_exit_bid_price'] - label['future_bid_price'] for label, row in known_pairs]
        summary['point_price_error_metrics'] = {'labeled_candidates': len(known_pairs),
            'original_mean_error': math.fsum(original_errors) / len(known_pairs) if known_pairs else None,
            'calibrated_mean_error': math.fsum(calibrated_errors) / len(known_pairs) if known_pairs else None,
            'original_mae': math.fsum(map(abs, original_errors)) / len(known_pairs) if known_pairs else None,
            'calibrated_mae': math.fsum(map(abs, calibrated_errors)) / len(known_pairs) if known_pairs else None,
            'basis': 'The same fixed selected candidate cohort and original labels; historical causal transforms, not untouched forward or unconditional calibration evidence.'}
        stage = 'treatment_fullstream'
        treatment, treatment_prefix = replay(treatment_candidates, treatment_source, TREATMENT_ARM, False)
        write_artifact(output / 'treatment-ledger.json', treatment)
        write_artifact(output / 'treatment-prefix-ledger.json', treatment_prefix)
        summary.update(arms={BASE_ARM: baseline, TREATMENT_ARM: treatment},
            prefix_ledgers={BASE_ARM: baseline_prefix, TREATMENT_ARM: treatment_prefix},
            arm_endpoints={BASE_ARM: arm_endpoints(baseline, baseline_prefix, base),
                           TREATMENT_ARM: arm_endpoints(treatment, treatment_prefix, base)},
            evaluation_status='evaluated', strategy_evaluated=True, comparison_valid=True)
    except Exception as exc:
        failure = exc
        summary['failure'] = {'stage': stage, 'type': type(exc).__name__, 'code': str(exc)}
    finally:
        attempts = []
        for phase in ARMS:
            collector = collectors.get(phase)
            if collector is not None:
                attempts.extend({'arm': phase, **row} for row in collector.attempt_evidence)
                summary['reconstruction_counters']['execution_evidence_original_projections'] += collector.original_projection_calls
        # Evidence overflow must not obscure the original failure or lose its
        # counters.  A failed member is never silently treated as retained.
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
                'by_saved_selector_status': {status: sum(r['quote_status'] == status for r in source.requests) for status in STATUSES}}
                for source in sources}
            retain('quote-witness.jsonl', [row for source in sources for row in source.witness])
        if artifact_failures:
            summary['artifact_failures'] = artifact_failures
        write_artifact(output / 'summary.json', summary)
    if failure is not None:
        raise ValueError('offline_price_calibration_evaluation_failed') from None
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-root', 'candidate-path', 'config', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    config = prior.load_json(prior.read_bounded(Path(args.config), 262144))
    evaluate(Path(args.source_root), Path(args.candidate_path), config, Path(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
