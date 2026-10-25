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
    row.update(run_name=f'run{index}', slug=f'market{index}', decision_ts=start + pd.Timedelta(seconds=124),
        entry_ts=start + pd.Timedelta(seconds=124), market_close_ts=start + pd.Timedelta(seconds=300),
        cash_release_ts=start + pd.Timedelta(seconds=1200), buy_up_price_2m=0.5, buy_down_price_2m=0.5,
        buy_up_size_2m=200.0, buy_down_size_2m=200.0, spread_up_median_first2m=0.02,
        spread_down_median_first2m=0.02, overround_median_first2m=0,
        outcome_up=float(index % 2), label_available_ts=start + pd.Timedelta(seconds=300))
    return row


def test_end_to_end_report_contract_and_empty_execution(monkeypatch, tmp_path):
    frame = pd.DataFrame([one_row(i) for i in range(4)])
    counts = {'fit': 0, 'predict': 0}
    class Model:
        def __init__(self, **kwargs):
            self.classes_ = [0, 1]
        def fit(self, x, y):
            counts['fit'] += 1
            self.feature_names_in_ = np.array(x.columns)
            assert len(x) == 2
        def predict_proba(self, x):
            counts['predict'] += 1
            return np.tile([0.1, 0.9], (len(x), 1))
    monkeypatch.setattr(audit, 'ExtraTreesClassifier', Model)
    monkeypatch.setattr(audit, 'build_dataset', lambda *args, **kw: (frame, {}, {'feature_rows': 4}))
    monkeypatch.setattr(audit, 'chronological_split', lambda *args, **kw:
        (frame.iloc[:2], frame.iloc[2:3], frame.iloc[3:], {'disjoint_slugs': True}))
    monkeypatch.setattr(audit.subprocess, 'check_output', lambda *args, **kw: 'a' * 40)
    report = audit.evaluate(tmp_path / 'repo' / 'data' / 'monthly_runs', config(), tmp_path / 'out')
    assert counts == {'fit': 1, 'predict': 1}
    assert len(report['rows']) == 4
    assert all(row['fills'] == 0 and not row['first_ten_passed'] for row in report['rows'])
    assert report['promotion_eligible'] is False and report['fresh_holdout'] is False
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
