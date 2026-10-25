"""Cloud-only fixed-policy audit with prefix inputs and an explicit cash ledger.

Experiment settings and findings belong in private input and encrypted output.
This entrypoint fits once before two chronological, disjoint scoring periods.
The reused archive and sampled-price labels cannot establish fresh or live
performance, regardless of the simulated return.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier

try:
    from .causal_prefix_dataset import FEATURES, build_dataset, chronological_split
    from .causal_cash_ledger import replay_cash_ledger
except ImportError:
    from causal_prefix_dataset import FEATURES, build_dataset, chronological_split
    from causal_cash_ledger import replay_cash_ledger


def validate_config(config: dict) -> None:
    if config.get('live_orders_enabled') is not False:
        raise ValueError('OFFLINE_CONFIG_REQUIRED')
    if not 1 <= len(config['profiles']) <= 2 or len({p['name'] for p in config['profiles']}) != len(config['profiles']):
        raise ValueError('PROFILE_BUDGET')
    if config['model_params'].get('n_jobs') not in (1, 2):
        raise ValueError('CPU_BUDGET')
    if not 1 <= config['model_params']['n_estimators'] <= 200:
        raise ValueError('ESTIMATOR_BUDGET')
    grid = config['stress_grid']
    cells = len(grid['slippages']) * len(grid['latencies_seconds']) * len(grid['depth_haircuts'])
    if not 1 <= cells <= 24:
        raise ValueError('STRESS_BUDGET')
    if any(not np.isfinite(v) or v < 0 for k in ('slippages', 'latencies_seconds') for v in grid[k]):
        raise ValueError('INVALID_EXECUTION_STRESS')
    if any(not np.isfinite(v) or not 0 < v <= 1 for v in grid['depth_haircuts']):
        raise ValueError('INVALID_DEPTH_STRESS')
    if config['required_fills'] != 10 or config['initial_cash'] != 10:
        raise ValueError('COMPOUND_TARGET_MISMATCH')


def signal_frame(frame: pd.DataFrame, probabilities: np.ndarray, gate: dict, fee_rate: float) -> pd.DataFrame:
    """Select using prefix features and frozen predictions only."""
    scored = frame.copy()
    scored['q_up'] = probabilities
    scored['side'] = np.where(scored.q_up >= 0.5, 'up', 'down')
    scored['q_side'] = np.maximum(scored.q_up, 1.0 - scored.q_up)
    price = np.where(scored.side.eq('up'), scored.buy_up_price_2m, scored.buy_down_price_2m)
    depth = np.where(scored.side.eq('up'), scored.buy_up_size_2m, scored.buy_down_size_2m)
    spread = np.where(scored.side.eq('up'), scored.spread_up_median_first2m, scored.spread_down_median_first2m)
    # Cost filtering is repeated at the actual delayed quote.  These prefix
    # gates never inspect an exit, a future completeness flag, or a label.
    eligible = (scored.q_side.ge(gate['confidence_floor'])
        & pd.Series(price, index=scored.index).between(gate['price_min'], gate['price_max'])
        & (depth >= gate['min_depth']) & (spread <= gate['max_spread'])
        & scored.overround_median_first2m.le(gate['max_overround'])
        & (scored.q_side - price - fee_rate * price * (1.0 - price)).ge(gate['edge_threshold']))
    return scored.loc[eligible].copy()


def execution_candidates(signals: pd.DataFrame, books: dict, *, slippage: float,
                         latency: float, gate: dict, fee_rate: float,
                         max_quote_wait_seconds: float) -> tuple[pd.DataFrame, dict]:
    rows, reasons = [], Counter()
    for row in signals.to_dict('records'):
        book = books.get(row['slug'])
        arrive = row['decision_ts'] + pd.Timedelta(seconds=latency)
        close = row['market_close_ts']
        if book is None or book.empty:
            reasons['entry_quote_unavailable'] += 1
            continue
        quote = book.loc[(book.ts_utc >= arrive)
            & (book.ts_utc <= arrive + pd.Timedelta(seconds=max_quote_wait_seconds))
            & (book.ts_utc < close)].sort_values('ts_utc', kind='mergesort').head(1)
        if quote.empty:
            reasons['entry_quote_unavailable'] += 1
            continue
        q = quote.iloc[0]
        side = row['side']
        values = pd.to_numeric(pd.Series([q.get(f'buy_{side}_cents'), q.get(f'sell_{side}_cents'),
            q.get(f'buy_{side}_size'), q.get('mid_up_cents'), q.get('mid_down_cents')]), errors='coerce')
        if not np.isfinite(values).all() or bool(q.get('quote_conflict', False)):
            reasons['entry_book_invalid'] += 1
            continue
        price, bid, depth, mid_up, mid_down = values.to_numpy(dtype=float)
        price, bid = price / 100, bid / 100
        fill = price + slippage
        if not (0 < bid <= price < 1 and fill < 1 and depth >= gate['min_depth']
                and gate['price_min'] <= price <= gate['price_max']
                and price - bid <= gate['max_spread']
                and (mid_up + mid_down - 100) / 100 <= gate['max_overround']
                and row['q_side'] - fill - fee_rate * fill * (1 - fill) >= gate['edge_threshold']):
            reasons['arrival_book_outside_frozen_gate'] += 1
            continue
        rows.append({**row, 'entry_ts': q.ts_utc, 'market_end_ts': close,
            'price': price, 'fill_price': fill, 'depth': depth, 'slippage': slippage})
    return pd.DataFrame(rows), {'signals': len(signals), 'executable_candidates': len(rows), 'rejected': dict(reasons)}


def evaluate(source_root: Path, config: dict, output: Path) -> dict:
    validate_config(config)
    observed = subprocess.check_output(['git', '-C', str(source_root.resolve().parents[1]), 'rev-parse', 'HEAD'], text=True).strip()
    if observed != config['source_commit']:
        raise ValueError('SOURCE_COMMIT_MISMATCH')
    features, books, quality = build_dataset(source_root,
        max_source_bytes=config['max_source_bytes'], max_snapshot_age_seconds=config['max_snapshot_age_seconds'],
        max_label_delay_seconds=config['max_label_delay_seconds'])
    # Persist input evidence before a split/model can fail. A zero-row cohort is
    # actionable source evidence and must not disappear behind an exception.
    output.mkdir(parents=True, exist_ok=True)
    preflight = {'format': config['delivery_contract']['expected_format'],
        'hypothesis_id': config['hypothesis_id'], 'source_commit': config['source_commit'],
        'observed_source_commit': observed, 'feature_rows': len(features), 'quality': quality,
        'split': {}, 'aggregate': [], 'rows': [],
        'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'strategy_evaluated': False, 'model_fit_calls': 0, 'model_predict_calls': 0,
        'ledger_replays': 0, 'execution_mode': config.get('execution_mode', 'research'),
        'stage': 'source_admission_completed', 'actual_fills': 0, 'verified_canary_fills': 0,
        'live_orders_enabled': False, 'promotion_eligible': False}
    (output / 'summary.json').write_text(json.dumps(preflight, indent=2, allow_nan=False) + '\n')
    if config.get('execution_mode') == 'data_diagnostic':
        return preflight
    train, selection, holdout, split = chronological_split(features,
        minimum_training_runs=config['minimum_training_runs'], selection_runs=config['selection_runs'])
    preflight.update(split=split, stage='chronological_split_completed')
    (output / 'summary.json').write_text(json.dumps(preflight, indent=2, allow_nan=False) + '\n')
    if len(train) < config['minimum_training_rows'] or train.outcome_up.nunique() < 2:
        raise ValueError('INSUFFICIENT_MATURE_TRAINING_LABELS')
    model = ExtraTreesClassifier(**config['model_params'])
    model.fit(train[list(FEATURES)], train.outcome_up.astype(int))
    if list(model.feature_names_in_) != list(FEATURES):
        raise ValueError('FITTED_FEATURE_CONTRACT')
    scored = pd.concat([selection.assign(audit_split='selection'), holdout.assign(audit_split='retrospective_holdout')])
    class_up = list(model.classes_).index(1)
    probability = model.predict_proba(scored[list(FEATURES)])[:, class_up]
    signals = signal_frame(scored, probability, config['signal_gate'], config['fee_rate'])
    rows = []
    gate_receipts = []
    for slippage in config['stress_grid']['slippages']:
        for latency in config['stress_grid']['latencies_seconds']:
            candidates, gates = execution_candidates(signals, books, slippage=slippage, latency=latency,
                gate=config['signal_gate'], fee_rate=config['fee_rate'], max_quote_wait_seconds=config['max_quote_wait_seconds'])
            gate_receipts.append({'slippage': slippage, 'latency_seconds': latency, **gates})
            for split_name in ('selection', 'retrospective_holdout'):
                current = candidates.loc[candidates.audit_split.eq(split_name)] if not candidates.empty else candidates
                for haircut in config['stress_grid']['depth_haircuts']:
                    for profile in config['profiles']:
                        ledger = replay_cash_ledger(current, books, initial_cash=config['initial_cash'],
                            base_fraction=profile['base_fraction'], boost_fraction=profile.get('boost_fraction'),
                            boost_after_profits=profile.get('boost_after_profits', 1), fee_rate=config['fee_rate'],
                            minimum_notional=config['minimum_order_usd'], minimum_shares=config['minimum_order_shares'],
                            depth_haircut=haircut, exit_cutoff_seconds=config['exit_cutoff_seconds'],
                            exit_latency_seconds=latency, max_exit_quote_wait_seconds=config['max_quote_wait_seconds'],
                            cash_release_delay_seconds=config['cash_release_delay_seconds'], required_fills=config['required_fills'])
                        rows.append({'split': split_name, 'profile': profile['name'], 'slippage': slippage,
                            'latency_seconds': latency, 'depth_haircut': haircut, **ledger})
    report = {'format': config['delivery_contract']['expected_format'], 'hypothesis_id': config['hypothesis_id'],
        'source_commit': config['source_commit'], 'observed_source_commit': observed,
        'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'feature_rows': len(features), 'quality': quality, 'split': split,
        'model_fit_calls': 1, 'model_predict_calls': 1, 'model_frozen_before_selection': True,
        'feature_names_in': list(model.feature_names_in_), 'execution_gates': gate_receipts,
        'exit_execution_contract': 'first eligible bid triggers breakeven-limit order; revalidate first delayed arrival quote, retain unfilled positions',
        'rows': rows, 'profiles': config['profiles'], 'strategy_evaluated': True,
        'live_orders_enabled': False, 'actual_fills': 0, 'verified_canary_fills': 0,
        'promotion_eligible': False, 'fresh_holdout': False,
        'evidence_boundary': 'retrospective archive replay; sampled-price proxy labels; assumed cash-release delay; quote crossing is not an authenticated fill',
        'decision': 'review_paired_retrospective_rows_no_live_promotion'}
    report['aggregate'] = [
        {'split': split_name, 'profile': profile['name'], 'cells': len(group),
         'passed_cells': sum(bool(r['first_ten_passed']) for r in group),
         'minimum_ending_cash': min(r['ending_cash'] for r in group),
         'maximum_ending_cash': max(r['ending_cash'] for r in group),
         'minimum_resolved_fills': min(r['resolved_fills'] for r in group),
         'maximum_resolved_fills': max(r['resolved_fills'] for r in group),
         'failure_counts': dict(Counter(str(r['failure_reason']) for r in group))}
        for split_name in ('selection', 'retrospective_holdout')
        for profile in config['profiles']
        for group in [[r for r in rows if r['split'] == split_name and r['profile'] == profile['name']]]
    ]
    output.mkdir(parents=True, exist_ok=True)
    (output / 'summary.json').write_text(json.dumps(report, indent=2, default=str, allow_nan=False) + '\n')
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.config.stat().st_size > 65536:
        raise ValueError('CONFIG_SIZE_LIMIT')
    result = evaluate(args.source_root, json.loads(args.config.read_text()), args.output)
    print(json.dumps({'format': result['format'], 'strategy_evaluated': result['strategy_evaluated']}))


if __name__ == '__main__':
    main()
