"""Frozen historical quote diagnostics over the complete UTC coverage ledger.

Reuses the existing fixed ridge/feature family. Price-only and depth-complete
cohorts are scored separately; every comparison within a cohort is paired.
This does not simulate execution, settlement, PnL, or account growth.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path

try:
    from archive_coverage_ledger import connect_readonly, inventory, load_dates, verify_sources, utc_us
    from archive_baseline_audit import write_private
    from quote_factor_diagnostic import fit, predict, validate_protocol
except ImportError:
    from analysis.archive_coverage_ledger import connect_readonly, inventory, load_dates, verify_sources, utc_us
    from analysis.archive_baseline_audit import write_private
    from analysis.quote_factor_diagnostic import fit, predict, validate_protocol


def file_hash(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def date_range(first, last):
    low, high = datetime.fromisoformat(first), datetime.fromisoformat(last)
    return [(low + timedelta(days=i)).date().isoformat() for i in range((high - low).days + 1)]


def validate_plan(p):
    cfg = p['diagnostic']
    validate_protocol(cfg)
    bins = p['quote_bin_edges_cents']
    if (len(bins) < 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in bins)
            or bins[0] != 0 or bins[-1] != 100 or any(a >= b for a, b in zip(bins, bins[1:]))):
        raise ValueError('invalid_quote_bins')
    validation_seen = set()
    for fold in p['folds']:
        train, test = fold['train_dates'], fold['validation_dates']
        local = dict(cfg, train_dates=train, validation_dates=test)
        validate_protocol(local)
        if train != sorted(train) or test != sorted(test):
            raise ValueError('unsorted_fold')
        if set(test) & validation_seen:
            raise ValueError('repeated_validation_market_dates')
        validation_seen.update(test)
        # Labels and features are strictly inside their own market, so the last
        # training target precedes the first validation day without shared rows.
        if max(train) >= min(test):
            raise ValueError('training_after_validation')
    if not p['folds']:
        raise ValueError('empty_folds')


def sample_window(rows, slug, start, date, cfg, cohort, bins, present,
                  price_conflict=False, depth_conflict=False, require_future_depth=False):
    item = {'market': slug, 'start': start, 'date': date, 'cohort': cohort,
            'decision_time': start + cfg['decision_offset_seconds'], 'features': None,
            'targets': None, 'status': None, 'quote_bin': 'unavailable'}
    needs_depth = cohort != 'common'
    if price_conflict or (needs_depth and depth_conflict):
        item['status'] = 'excluded_price_conflict' if price_conflict else 'excluded_depth_conflict'
        return item
    if not rows:
        item['status'] = 'present_invalid_time_only' if present else 'absent_window'
        return item
    times = [x['ts'] for x in rows]
    decision = item['decision_time']
    index = bisect.bisect_left(times, decision) - 1
    lag_cutoff = decision - cfg['lookback_seconds']
    lag_index = bisect.bisect_right(times, lag_cutoff) - 1
    chosen = []
    for name, idx, cutoff in [('decision', index, decision), ('lookback', lag_index, lag_cutoff)]:
        if idx < 0 or cutoff - times[idx] > cfg['max_quote_age_seconds']:
            item['status'] = 'excluded_' + name + '_missing_or_stale'
            return item
        row = rows[idx]
        if row['price_reason']:
            item.update(status='excluded_' + name + '_price', reason=row['price_reason'])
            return item
        if needs_depth and row['depth_reason']:
            item.update(status='excluded_' + name + '_depth', reason=row['depth_reason'])
            return item
        chosen.append(row)
    current, lag = chosen
    ask, bid, _, _, ask_size, bid_size, _, _ = current['values']
    item['features'] = {'momentum': (ask + bid - lag['values'][0] - lag['values'][1]) / 2,
                        'spread': ask - bid}
    if needs_depth:
        item['features']['imbalance'] = (bid_size - ask_size) / (bid_size + ask_size)
    item['feature_times'] = [lag['ts'], current['ts']]
    bin_index = min(len(bins) - 2, bisect.bisect_right(bins, ask) - 1)
    item['quote_bin'] = f'{bins[bin_index]}:{bins[bin_index + 1]}'
    future_index = bisect.bisect_left(times, decision + cfg['horizon_seconds'])
    if future_index == len(rows) or times[future_index] > decision + cfg['horizon_seconds'] + cfg['future_tolerance_seconds']:
        item['status'] = 'unknown_future_missing'
        return item
    future = rows[future_index]
    if future['price_reason']:
        item.update(status='unknown_future_price', reason=future['price_reason'])
        return item
    if require_future_depth and future['depth_reason']:
        item.update(status='excluded_future_depth_legacy_rule', future_price_observed=True,
                    reason=future['depth_reason'])
        return item
    item.update(status='labeled_quote_change', target_time=future['ts'],
                targets={'bid_change_cents': future['values'][1] - bid,
                         'ask_change_cents': future['values'][0] - ask})
    return item


def samples_for_dates(db, dates, cfg, bins, cohorts=('common', 'depth'), require_future_depth=False):
    result = {cohort: [] for cohort in cohorts}
    for date in sorted(set(dates)):
        grouped, pc, dc, present = load_dates(db, [date])
        first = utc_us(date + 'T00:00:00Z') // 1000000
        for start in range(first, first + 86400, 300):
            slug = f'btc-updown-5m-{start}'
            for cohort in cohorts:
                result[cohort].append(sample_window(grouped.get(slug, []), slug, start, date, cfg,
                    cohort, bins, slug in present, slug in pc, slug in dc, require_future_depth))
    return result


def coverage(rows):
    statuses = Counter(x['status'] for x in rows)
    return {'scheduled_windows': len(rows), 'status_counts': dict(sorted(statuses.items())),
            'feature_eligible_windows': sum(x['features'] is not None for x in rows),
            'labeled_windows': sum(x['targets'] is not None for x in rows),
            'unknown_future_quotes': sum(n for k, n in statuses.items() if k.startswith('unknown_future_')),
            'legacy_future_depth_exclusions': statuses['excluded_future_depth_legacy_rule']}


def metrics(rows, target, names):
    eligible = [x for x in rows if x.get('predictions') is not None]
    known = [x for x in eligible if x['targets'] is not None]
    unknown = [x for x in eligible if x['targets'] is None]
    result = {'coverage': coverage(rows), 'scored_labels': len(known),
              'predicted_unscored_labels': len(unknown),
              'predicted_unknown_future_quotes': sum(x['status'].startswith('unknown_future_') for x in unknown),
              'predicted_legacy_depth_exclusions': sum(x['status'] == 'excluded_future_depth_legacy_rule' for x in unknown),
              'models': {}}
    for name in names:
        errors = [x['predictions'][target][name] - x['targets'][target] for x in known]
        ss = sum(e * e for e in errors)
        model = {'mse': ss / len(known) if known else None,
                 'mae': sum(abs(e) for e in errors) / len(known) if known else None,
                 'mse_eligible_lower': ss / len(eligible) if eligible else None,
                 'mse_eligible_upper': (ss + sum((100 + abs(x['predictions'][target][name])) ** 2 for x in unknown)) / len(eligible) if eligible else None,
                 'paired_improvement_vs': {}}
        for baseline in ('zero', 'train_mean', 'momentum'):
            deltas = [(x['predictions'][target][baseline] - x['targets'][target]) ** 2 - e * e
                      for x, e in zip(known, errors)]
            low = high = sum(deltas)
            for x in unknown:
                a, b = x['predictions'][target][baseline], x['predictions'][target][name]
                ends = [(a - y) ** 2 - (b - y) ** 2 for y in (-100, 100)]
                low += min(ends)
                high += max(ends)
            model['paired_improvement_vs'][baseline] = {
                'observed_mean': sum(deltas) / len(known) if known else None,
                'eligible_lower': low / len(eligible) if eligible else None,
                'eligible_upper': high / len(eligible) if eligible else None,
                'n_common_labels': len(known)}
        result['models'][name] = model
    return result


def evaluate(samples, folds, cfg, cohort):
    models = {n: f for n, f in cfg['models'].items() if cohort != 'common' or 'imbalance' not in f}
    fold_reports, predictions = [], []
    for index, fold in enumerate(folds):
        train_dates, val_dates = set(fold['train_dates']), set(fold['validation_dates'])
        train_all = [x for x in samples if x['date'] in train_dates]
        val = [dict(x, fold=index) for x in samples if x['date'] in val_dates]
        train = [x for x in train_all if x['targets'] is not None and x['features'] is not None]
        report = {'fold': index, **fold, 'train_coverage': coverage(train_all), 'validation_coverage': coverage(val),
                  'status': 'evaluated', 'fits': {}}
        if len(train) < cfg['min_train_labels']:
            report['status'] = 'insufficient_training_labels'
            for row in val:
                row['predictions'] = None
        else:
            if coverage(val)['labeled_windows'] < cfg['min_validation_labels']:
                report['status'] = 'insufficient_validation_labels_descriptive_only'
            cutoff = utc_us(min(val_dates) + 'T00:00:00Z') / 1000000
            assert all(max(x['feature_times']) < x['decision_time'] and x['target_time'] < cutoff for x in train)
            for target in cfg['targets']:
                fits = {n: fit(train, target, fields, cfg['ridge_penalty']) for n, fields in models.items()}
                fits['zero']['intercept'] = 0.0
                report['fits'][target] = fits
            for row in val:
                row['predictions'] = {t: {n: predict(f, row) for n, f in fits.items()}
                                      for t, fits in report['fits'].items()} if row['features'] is not None else None
        predictions.extend(val)
        report['scores'] = {t: metrics(val, t, models) for t in cfg['targets']}
        fold_reports.append(report)
    groups = {'by_date': defaultdict(list), 'by_time_block': defaultdict(list), 'by_quote_bin': defaultdict(list)}
    for row in predictions:
        hour = datetime.fromtimestamp(row['start'], timezone.utc).hour
        groups['by_date'][row['date']].append(row)
        groups['by_time_block'][f"{row['date']}T{hour // cfg['stability_block_hours'] * cfg['stability_block_hours']:02d}"].append(row)
        groups['by_quote_bin'][row['quote_bin']].append(row)
    return {'cohort': cohort, 'models': models, 'folds': fold_reports,
            'overall': {t: metrics(predictions, t, models) for t in cfg['targets']},
            **{name: {key: {t: metrics(rows, t, models) for t in cfg['targets']}
                      for key, rows in sorted(values.items())} for name, values in groups.items()},
            'window_ledger': predictions,
            'comparison_scope': 'Each candidate/baseline uses identical training/validation rows within this cohort; cross-cohort MSE differences are not factor increments.',
            'missingness_scope': 'Conditional observed-label errors; unknown-label bounds only cover predicted eligible windows, not excluded/absent windows.'}


def run(database, root, plan):
    validate_plan(plan)
    if file_hash(database) != plan['database_sha256']:
        raise ValueError('ledger_hash_changed')
    for path, expected in plan['code_sha256'].items():
        if file_hash(Path(__file__).resolve().parents[1] / path) != expected:
            raise ValueError('frozen_code_changed')
    with connect_readonly(database) as db:
        if db.execute("SELECT value FROM meta WHERE key='inventory_sha256'").fetchone()[0] != plan['inventory_sha256']:
            raise ValueError('inventory_digest_changed')
        files = inventory(db)
        verify_sources(root, files)
        dates = sorted({d for fold in plan['folds'] for key in ('train_dates', 'validation_dates') for d in fold[key]})
        cfg, bins = plan['diagnostic'], plan['quote_bin_edges_cents']
        samples = samples_for_dates(db, dates, cfg, bins)
        results = {cohort: evaluate(rows, plan['folds'], cfg, cohort) for cohort, rows in samples.items()}
        corrections = {}
        for correction in plan.get('corrections', []):
            fold = {key: correction[key] for key in ('train_dates', 'validation_dates')}
            rows = samples_for_dates(db, fold['train_dates'] + fold['validation_dates'], cfg, bins,
                                     cohorts=('legacy_depth',), require_future_depth=True)['legacy_depth']
            report = evaluate(rows, [fold], cfg, 'legacy_depth')
            if report['folds'][0]['fits'] != correction['original_fits']:
                raise ValueError('correction_training_changed')
            corrections[correction['id']] = {'scope': 'input-correction version, not independent OOS', **report}
        verify_sources(root, files)
    return {'scope': 'historically exposed walk-forward exploration, not certified unseen OOS',
            'plan': plan, 'cohorts': results, 'corrections': corrections, 'source_bytes_unchanged': True,
            'real_fills': 0, 'pnl': None, 'canary_gate_changed': False,
            'limitations': ['source and receive times unverified', 'market slug is not authenticated token identity',
                            'daily/market blocks not proved independent', 'no tuning or winner selection',
                            'no significance or causal claim', 'no depth imputation; future labels never features']}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--poly-root', type=Path, required=True)
    p.add_argument('--protocol', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(argv)
    result = run(a.database, a.poly_root, json.loads(a.protocol.read_text()))
    result['protocol_sha256'] = file_hash(a.protocol)
    write_private(a.output, result)
    print('PRIVATE_WALK_FORWARD_COMPLETE; no empirical contents printed.')


if __name__ == '__main__':
    main()
