import json

import numpy as np
import pandas as pd
import pytest

from analysis import causal_repair_audit as audit


def config():
    return {'live_orders_enabled': False, 'profiles': [{'name': 'control', 'base_fraction': 0.5},
        {'name': 'treatment', 'base_fraction': 0.5, 'boost_fraction': 0.6}],
        'model_params': {'n_jobs': 1, 'n_estimators': 5},
        'stress_grid': {'slippages': [0.01], 'latencies_seconds': [2], 'depth_haircuts': [0.5]},
        'required_fills': 10, 'initial_cash': 10, 'source_commit': 'a' * 40,
        'max_source_bytes': 100, 'max_snapshot_age_seconds': 10, 'max_label_delay_seconds': 5,
        'minimum_training_runs': 1, 'selection_runs': 1, 'minimum_training_rows': 2,
        'signal_gate': {'confidence_floor': 0.7, 'price_min': 0.1, 'price_max': 0.8,
            'min_depth': 10, 'max_spread': 0.06, 'max_overround': 0.03, 'edge_threshold': 0.01},
        'fee_rate': 0.07, 'max_quote_wait_seconds': 5, 'minimum_order_usd': 5,
        'minimum_order_shares': 5, 'exit_cutoff_seconds': 30, 'cash_release_delay_seconds': 900,
        'hypothesis_id': 'synthetic', 'delivery_contract': {'expected_format': 'synthetic-v1'}}


def one_row(index):
    start = pd.Timestamp('2020-01-01', tz='UTC') + pd.Timedelta(seconds=index * 1800)
    row = {name: 1.0 for name in audit.FEATURES}
    row.update(run_name=f'run{index}', slug=f'market{index}', market_start_ts=start,
        decision_ts=start + pd.Timedelta(seconds=124),
        entry_ts=start + pd.Timedelta(seconds=124), market_close_ts=start + pd.Timedelta(seconds=300),
        cash_release_ts=start + pd.Timedelta(seconds=1200), buy_up_price_2m=0.5, buy_down_price_2m=0.5,
        buy_up_size_2m=200.0, buy_down_size_2m=200.0, spread_up_median_first2m=0.02,
        spread_down_median_first2m=0.02, overround_median_first2m=0,
        outcome_up=float(index % 2), label_available_ts=start + pd.Timedelta(seconds=300))
    return row


@pytest.mark.parametrize('feature_profile', ['full_prefix_v1', 'price_book_v1'])
def test_end_to_end_report_contract_and_empty_execution(monkeypatch, tmp_path, feature_profile):
    frame = pd.DataFrame([one_row(i) for i in range(4)])
    value = config()
    value['feature_profile'] = feature_profile
    expected_features, _ = audit.feature_contract(feature_profile)
    if feature_profile == 'price_book_v1':
        frame = frame.drop(columns=['trade_count_sum_first2m', 'trade_volume_sum_first2m'])
    counts = {'fit': 0, 'predict': 0}
    class Model:
        def __init__(self, **kwargs):
            self.classes_ = [0, 1]
        def fit(self, x, y):
            counts['fit'] += 1
            self.feature_names_in_ = np.array(x.columns)
            assert len(x) == 2
            assert tuple(x.columns) == expected_features
        def predict_proba(self, x):
            counts['predict'] += 1
            assert tuple(x.columns) == expected_features
            return np.tile([0.1, 0.9], (len(x), 1))
    monkeypatch.setattr(audit, 'ExtraTreesClassifier', Model)
    monkeypatch.setattr(audit, 'build_dataset', lambda *args, **kw: (frame, {}, {'feature_rows': 4}))
    monkeypatch.setattr(audit, 'chronological_split', lambda *args, **kw:
        (frame.iloc[:2], frame.iloc[2:3], frame.iloc[3:], {'disjoint_slugs': True}))
    monkeypatch.setattr(audit.subprocess, 'check_output', lambda *args, **kw: 'a' * 40)
    report = audit.evaluate(tmp_path / 'repo' / 'data' / 'monthly_runs', value, tmp_path / 'out')
    assert counts == {'fit': 1, 'predict': 1}
    assert len(report['rows']) == 4
    assert all(row['fills'] == 0 and not row['first_ten_passed'] for row in report['rows'])
    assert report['promotion_eligible'] is False and report['fresh_holdout'] is False
    assert report['registered_feature_names'] == list(expected_features)
    assert report['feature_profile'] == feature_profile
    assert json.loads((tmp_path / 'out' / 'summary.json').read_text())['actual_fills'] == 0


def test_signal_admission_does_not_use_outcome_or_future_availability():
    frame = pd.DataFrame([one_row(0)])
    before = audit.signal_frame(frame, np.array([0.9]), config()['signal_gate'], 0.07)
    frame['outcome_up'] = np.nan
    frame['label_available_ts'] = pd.NaT
    after = audit.signal_frame(frame, np.array([0.9]), config()['signal_gate'], 0.07)
    assert before.slug.tolist() == after.slug.tolist() == ['market0']


def test_execution_uses_first_arrival_quote_and_never_waits_for_better_price():
    frame = audit.signal_frame(pd.DataFrame([one_row(0)]), np.array([0.9]), config()['signal_gate'], 0.07)
    at = frame.iloc[0].decision_ts + pd.Timedelta(seconds=2)
    books = {'market0': pd.DataFrame([
        {'ts_utc': at, 'buy_up_cents': 99, 'sell_up_cents': 98, 'buy_up_size': 200, 'mid_up_cents': 98.5, 'mid_down_cents': 1.5},
        {'ts_utc': at + pd.Timedelta(seconds=1), 'buy_up_cents': 50, 'sell_up_cents': 49, 'buy_up_size': 200, 'mid_up_cents': 49.5, 'mid_down_cents': 50.5}])}
    rows, receipt = audit.execution_candidates(frame, books, slippage=0.01, latency=2,
        gate=config()['signal_gate'], fee_rate=0.07, max_quote_wait_seconds=5)
    assert rows.empty
    assert receipt['rejected']['arrival_book_outside_frozen_gate'] == 1


def test_rejects_expanded_grid_before_source_read():
    value = config()
    value['stress_grid']['slippages'] = [0.01] * 25
    with pytest.raises(ValueError, match='STRESS_BUDGET'):
        audit.validate_config(value)


def test_rejects_unregistered_feature_profile_before_source_read():
    value = config()
    value['feature_profile'] = 'unregistered'
    with pytest.raises(ValueError):
        audit.validate_config(value)


def test_zero_cohort_diagnostic_has_no_model_split_or_replay(monkeypatch, tmp_path):
    value = config()
    value['execution_mode'] = 'data_diagnostic'
    monkeypatch.setattr(audit, 'build_dataset', lambda *a, **kw:
        (pd.DataFrame(), {}, {'feature_rows': 0, 'quality_counts': {'synthetic_rejection': 2}}))
    monkeypatch.setattr(audit.subprocess, 'check_output', lambda *a, **kw: 'a' * 40)
    monkeypatch.setattr(audit, 'chronological_split', lambda *a, **kw: pytest.fail('diagnostic split'))
    monkeypatch.setattr(audit, 'ExtraTreesClassifier', lambda **kw: pytest.fail('diagnostic model'))
    result = audit.evaluate(tmp_path / 'repo/data/monthly_runs', value, tmp_path / 'out')
    assert result['feature_rows'] == 0 and result['strategy_evaluated'] is False
    assert result['model_fit_calls'] == result['ledger_replays'] == 0
    assert json.loads((tmp_path / 'out/summary.json').read_text())['quality']['quality_counts']


def test_split_failure_keeps_source_quality_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, 'build_dataset', lambda *a, **kw:
        (pd.DataFrame(), {}, {'quality_counts': {'synthetic_rejection': 2}}))
    monkeypatch.setattr(audit.subprocess, 'check_output', lambda *a, **kw: 'a' * 40)
    with pytest.raises(ValueError, match='no_prefix_rows'):
        audit.evaluate(tmp_path / 'repo/data/monthly_runs', config(), tmp_path / 'out')
    evidence = json.loads((tmp_path / 'out/summary.json').read_text())
    assert evidence['strategy_evaluated'] is False and evidence['model_fit_calls'] == 0
    assert evidence['quality']['quality_counts']['synthetic_rejection'] == 2


@pytest.mark.parametrize('failure,expected', [
    ('fit', ('model_fit_started', 1, 0, 0, 0, 0, 0)),
    ('feature_contract', ('model_fit_completed', 1, 1, 0, 0, 0, 0)),
    ('predict', ('model_prediction_started', 1, 1, 1, 0, 0, 0)),
    ('signals', ('model_prediction_completed', 1, 1, 1, 1, 0, 0)),
    ('second_ledger', ('ledger_replay_started', 1, 1, 1, 1, 2, 1)),
])
def test_stage_failure_keeps_completed_work_counters(monkeypatch, tmp_path, failure, expected):
    """Faults after fitting must not restore the original zero-work preflight."""
    frame = pd.DataFrame([one_row(i) for i in range(4)])
    class Model:
        classes_ = [0, 1]
        def __init__(self, **kwargs):
            pass
        def fit(self, x, y):
            if failure == 'fit':
                raise ValueError('synthetic_fit_failure')
            self.feature_names_in_ = np.array(['wrong'] if failure == 'feature_contract' else x.columns)
        def predict_proba(self, x):
            if failure == 'predict':
                raise ValueError('synthetic_prediction_failure')
            return np.tile([0.1, 0.9], (len(x), 1))
    monkeypatch.setattr(audit, 'ExtraTreesClassifier', Model)
    monkeypatch.setattr(audit, 'build_dataset', lambda *a, **kw: (frame, {}, {'feature_rows': 4}))
    monkeypatch.setattr(audit, 'chronological_split', lambda *a, **kw:
        (frame.iloc[:2], frame.iloc[2:3], frame.iloc[3:], {'disjoint_slugs': True}))
    monkeypatch.setattr(audit.subprocess, 'check_output', lambda *a, **kw: 'a' * 40)
    if failure == 'signals':
        def broken_signals(*args, **kwargs):
            raise ValueError('synthetic_signal_failure')
        monkeypatch.setattr(audit, 'signal_frame', broken_signals)
    if failure == 'second_ledger':
        replay = audit.replay_cash_ledger
        calls = []
        def broken_replay(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise ValueError('synthetic_second_ledger_failure')
            return replay(*args, **kwargs)
        monkeypatch.setattr(audit, 'replay_cash_ledger', broken_replay)
    with pytest.raises(ValueError, match='synthetic_|FITTED_FEATURE_CONTRACT'):
        audit.evaluate(tmp_path / 'repo/data/monthly_runs', config(), tmp_path / 'out')
    evidence = json.loads((tmp_path / 'out/summary.json').read_text())
    actual = tuple(evidence[key] for key in ('stage', 'model_fit_calls', 'model_fit_completions',
        'model_predict_calls', 'model_predict_completions', 'ledger_replay_calls', 'ledger_replays'))
    assert actual == expected
    assert evidence['strategy_evaluated'] is False
    assert evidence['rows'] == [] and evidence['aggregate'] == []
    assert evidence['actual_fills'] == evidence['verified_canary_fills'] == 0
    assert not (tmp_path / 'out/summary.json.pending').exists()


def test_evidence_horizon_uses_all_registered_books_and_caps_selection():
    frame = pd.DataFrame([one_row(0), one_row(1)])
    start = frame.iloc[0].market_start_ts
    books = {'market0': pd.DataFrame({'ts_utc': [pd.NaT, start + pd.Timedelta(seconds=200)]}),
             'market1': pd.DataFrame({'ts_utc': [start + pd.Timedelta(seconds=2500)]}),
             'unregistered': pd.DataFrame({'ts_utc': [start + pd.Timedelta(seconds=9999)]})}
    boundary = start + pd.Timedelta(seconds=2400)
    horizon, receipt = audit.split_evidence_horizon(frame, books, boundary=boundary)
    assert horizon == boundary
    assert receipt['maximum_observed_quote_ts'] == (start + pd.Timedelta(seconds=2500)).isoformat()
    assert receipt['registered_slug_count'] == receipt['books_with_valid_quote_clock'] == 2
    assert receipt['source'] == 'registered_books_max_observed_quote_ts'
    assert receipt['scope'] == 'all_registered_split_slugs_before_signal_selection'


def test_empty_books_horizon_fallback_is_explicit_and_does_not_invent_release():
    frame = pd.DataFrame([one_row(0), one_row(1)])
    horizon, receipt = audit.split_evidence_horizon(frame, {})
    assert horizon == frame.market_close_ts.max()
    assert receipt['source'] == 'empty_book_fallback'
    assert receipt['maximum_observed_quote_ts'] is None
    assert receipt['derived_from_outcomes_or_assumed_releases'] is False


def test_position_release_after_raw_evidence_horizon_remains_locked():
    row = one_row(0)
    start = row['market_start_ts']
    row.update(side='up', fill_price=0.5, depth=200, slippage=0.0,
               market_end_ts=row['market_close_ts'])
    book = pd.DataFrame([{'ts_utc': start + pd.Timedelta(seconds=200),
                          'sell_up_cents': 80.0, 'buy_up_cents': 82.0, 'sell_up_size': 200.0}])
    frame = pd.DataFrame([row])
    horizon, _ = audit.split_evidence_horizon(frame, {'market0': book})
    result = audit.replay_cash_ledger(frame, {'market0': book}, evidence_end_ts=horizon)
    assert result['fills'] == result['unresolved_fills'] == 1
    assert result['resolved_fills'] == 0
    assert result['released_payout_total'] == 0.0
    assert result['ending_cash'] < result['initial_cash']
    assert result['chain'][0]['state'] == 'entered_unresolved'
