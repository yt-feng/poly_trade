"""Offline archive inventory and quote-only baseline diagnostics.

Reads caller-supplied protocol, never fits or selects a winning rule. Results
must be written outside the checkout, then sealed before publication. No order,
account, credential, network, or authenticated feed APIs are imported.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

PRICE_FIELDS = tuple(f'{action}_{side}_cents' for side in ('up', 'down') for action in ('buy', 'sell'))
SIZE_FIELDS = tuple(f'{action}_{side}_size' for side in ('up', 'down') for action in ('buy', 'sell'))
AUDIT_FIELDS = PRICE_FIELDS + SIZE_FIELDS + ('target_price', 'final_price', 'trade_count_1s', 'trade_volume_1s')
PROVENANCE_FIELDS = ('received_at_ns', 'source_event_ms', 'fee_rate', 'min_order_size', 'tick_size', 'condition_id')
STRATEGIES = {'no_trade', 'fixed_up', 'fixed_down', 'mid_momentum'}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def market_start(row):
    slug = row.get('slug', '') or str(row.get('market_url', '')).rstrip('/').split('/')[-1]
    if not re.fullmatch(r'btc-updown-5m-\d{10}', slug):
        return None, None
    start = int(slug.rsplit('-', 1)[1])
    return (slug, start) if start % 300 == 0 else (None, None)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def revision(root):
    return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()


def validate_protocol(p):
    if not isinstance(p.get('baselines'), list) or not p['baselines'] or set(p['baselines']) - STRATEGIES:
        raise ValueError('INVALID_BASELINES')
    for name in ('reference_days', 'evaluation_days', 'decision_offset_seconds', 'signal_lookback_seconds',
                 'delay_seconds', 'hold_seconds', 'max_sample_gap_seconds'):
        if type(p.get(name)) is not int or p[name] <= 0:
            raise ValueError('INVALID_PROTOCOL_INTEGER')
    if not 0 < p['signal_lookback_seconds'] < p['decision_offset_seconds'] < 300:
        raise ValueError('INVALID_SIGNAL_TIMING')
    for name in ('initial_cash_usd', 'slippage_per_leg_usd'):
        if number(p.get(name)) is None or p[name] <= 0:
            raise ValueError('INVALID_PROTOCOL_COST')
    for n in p['minimum_shares_scenarios']:
        if type(n) is not int or n <= 0:
            raise ValueError('INVALID_MINIMUM_SHARES')
    for n in p['fee_per_share_per_leg_usd_scenarios']:
        if number(n) is None or n <= 0:
            raise ValueError('MISSING_POSITIVE_FEE_SCENARIO')
    return p


def inventory(root, paths):
    """Full header/missingness/time audit; no settlement-label inference."""
    files, dates, windows = [], Counter(), set()
    missing, invalid, schema_absent = Counter(), Counter(), Counter()
    totals = Counter()
    first, last = None, None
    for path in paths:
        content = path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
        fields = reader.fieldnames or []
        file_missing, file_invalid, file_dates, file_windows = Counter(), Counter(), Counter(), set()
        n, fmin, fmax, prior_ts = 0, None, None, None
        seen, intervals = set(), Counter()
        for row in reader:
            n += 1
            for field in AUDIT_FIELDS:
                if not str(row.get(field, '')).strip():
                    file_missing[field] += 1
                elif number(row[field]) is None:
                    file_invalid[field] += 1
            ts = timestamp(row.get('ts_iso'))
            slug, start = market_start(row)
            if ts is None:
                totals['invalid_or_naive_timestamp_rows'] += 1
                continue
            fmin = ts if fmin is None else min(fmin, ts)
            fmax = ts if fmax is None else max(fmax, ts)
            file_dates[day(ts)] += 1
            if slug is None:
                totals['invalid_or_non_btc_window_rows'] += 1
                continue
            file_windows.add(slug)
            if not 0 <= ts - start < 300:
                totals['outside_slug_start_plus_300s_rows'] += 1
            if row.get('close_ts_utc') and timestamp(row['close_ts_utc']) == start:
                totals['derived_close_equals_actual_start_rows'] += 1
            key = (slug, ts)
            if key in seen:
                totals['within_file_duplicate_timestamp_rows'] += 1
            seen.add(key)
            if prior_ts is not None and prior_ts[0] == slug:
                gap = ts - prior_ts[1]
                intervals[str(gap)] += 1
                if gap < 0:
                    totals['within_file_time_reversals'] += 1
            prior_ts = key
        for field in PROVENANCE_FIELDS:
            if field not in fields:
                schema_absent[field] += n
        totals['rows'] += n
        missing.update(file_missing)
        invalid.update(file_invalid)
        dates.update(file_dates)
        windows.update(file_windows)
        first = fmin if first is None else min(first, fmin) if fmin is not None else first
        last = fmax if last is None else max(last, fmax) if fmax is not None else last
        files.append({'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(content).hexdigest(),
                      'bytes': len(content), 'rows': n, 'fields': fields, 'min_ts': fmin, 'max_ts': fmax,
                      'utc_row_dates': dict(file_dates), 'distinct_windows': len(file_windows),
                      'missing': dict(file_missing), 'invalid_numeric': dict(file_invalid),
                      'within_file_gap_seconds_histogram': dict(intervals)})
    return {'files': files, 'totals': dict(totals), 'distinct_windows': len(windows),
            'utc_row_dates': dict(sorted(dates.items())), 'utc_window_dates': sorted({day(int(s.rsplit('-', 1)[1])) for s in windows}),
            'min_ts': first, 'max_ts': last, 'missing': dict(missing), 'invalid_numeric': dict(invalid),
            'provenance_fields_absent_rows': dict(schema_absent),
            'global_duplicate_scope': 'Cross-file deduplication/conflicts are measured on selected replay dates only; do not sum file window counts.'}


def load_selected(root, file_manifest, selected_dates):
    records, conflicts = {}, set()
    counts = Counter()
    for info in file_manifest:
        # Window date and receipt date can straddle UTC midnight. Include adjacent
        # files conservatively instead of filtering only on the row date.
        if info['min_ts'] is None or not selected_dates:
            continue
        low = timestamp(min(selected_dates) + 'T00:00:00Z')
        high = timestamp(max(selected_dates) + 'T00:00:00Z') + 86400
        if info['max_ts'] < low or info['min_ts'] >= high + 300:
            continue
        with (root / info['path']).open(encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                slug, start = market_start(row)
                ts = timestamp(row.get('ts_iso'))
                if start is None or ts is None or day(start) not in selected_dates:
                    continue
                counts['selected_rows_before_dedup'] += 1
                if not 0 <= ts - start < 300:
                    counts['outside_window_rows_excluded'] += 1
                    continue
                key = (slug, ts)
                fields = tuple(number(row.get(name)) for name in PRICE_FIELDS + SIZE_FIELDS)
                if key in records:
                    counts['duplicate_timestamp_rows'] += 1
                    if records[key] != fields:
                        conflicts.add(slug)
                        counts['conflicting_timestamp_rows'] += 1
                else:
                    records[key] = fields
    windows = defaultdict(list)
    for (slug, ts), fields in records.items():
        if slug in conflicts:
            continue  # A conflicting observation disqualifies the entire window.
        q = dict(zip(PRICE_FIELDS + SIZE_FIELDS, fields))
        q['ts'] = ts
        windows[slug].append(q)
    gaps = Counter()
    for rows in windows.values():
        rows.sort(key=lambda r: r['ts'])
        gaps.update(str(b['ts'] - a['ts']) for a, b in zip(rows, rows[1:]))
    counts['conflicting_windows_excluded'] = len(conflicts)
    counts['selected_distinct_timestamps'] = len(records)
    return dict(windows), {'counts': dict(counts), 'deduplicated_gap_seconds_histogram': dict(gaps)}


def choose_at(rows, when, tolerance, *, before=False):
    times = [r['ts'] for r in rows]
    index = bisect.bisect_right(times, when) - 1 if before else bisect.bisect_left(times, when)
    if index < 0 or index >= len(rows) or abs(rows[index]['ts'] - when) > tolerance:
        return None
    return rows[index]


def valid_book(row, side):
    if row is None:
        return False
    ask, bid = row.get(f'buy_{side}_cents'), row.get(f'sell_{side}_cents')
    return ask is not None and bid is not None and 0 < bid <= ask < 100


def side_for(strategy, rows, start, p):
    if strategy == 'no_trade':
        return None
    if strategy in ('fixed_up', 'fixed_down'):
        return strategy.split('_')[1]
    at = start + p['decision_offset_seconds']
    recent = choose_at(rows, at, p['max_sample_gap_seconds'], before=True)
    earlier = choose_at(rows, at - p['signal_lookback_seconds'], p['max_sample_gap_seconds'], before=True)
    if not valid_book(recent, 'up') or not valid_book(earlier, 'up'):
        return None
    change = recent['buy_up_cents'] + recent['sell_up_cents'] - earlier['buy_up_cents'] - earlier['sell_up_cents']
    return 'up' if change > 0 else 'down' if change < 0 else None


def replay(windows, p, strategy, shares, fee):
    cash = float(p['initial_cash_usd'])
    peak = cash
    max_drawdown = 0.0
    ledger, reasons = [], Counter()
    halted = False
    busy_until = -math.inf
    for slug, rows in sorted(windows.items(), key=lambda item: int(item[0].rsplit('-', 1)[1])):
        start = int(slug.rsplit('-', 1)[1])
        decision = start + p['decision_offset_seconds']
        item = {'window': slug, 'provenance': 'paper_simulation', 'real_fill': False, 'cash_before_usd': cash}
        reason = None
        side = side_for(strategy, rows, start, p)
        if halted:
            reason = 'halted_unreconciled_exit'
        elif decision < busy_until:
            reason = 'position_still_open'
        elif side is None:
            reason = 'no_trade_policy' if strategy == 'no_trade' else 'no_causal_signal'
        else:
            entry = choose_at(rows, decision + p['delay_seconds'], p['max_sample_gap_seconds'])
            if not valid_book(entry, side):
                reason = 'missing_or_invalid_entry_book'
            elif entry[f'buy_{side}_size'] is None or entry[f'buy_{side}_size'] < shares:
                reason = 'insufficient_or_unknown_entry_depth'
            else:
                price = entry[f'buy_{side}_cents'] / 100 + p['slippage_per_leg_usd']
                cost = shares * (price + fee)
                if price >= 1:
                    reason = 'entry_price_outside_contract_range'
                elif cost > cash + 1e-12:
                    reason = 'minimum_shares_unaffordable'
                else:
                    cash -= cost
                    exit_at = entry['ts'] + p['hold_seconds'] + p['delay_seconds']
                    exit_row = choose_at(rows, exit_at, p['max_sample_gap_seconds'])
                    reconciled = (exit_at < start + 300 and valid_book(exit_row, side)
                                  and exit_row[f'sell_{side}_size'] is not None
                                  and exit_row[f'sell_{side}_size'] >= shares)
                    proceeds = 0.0
                    if reconciled:
                        price_out = max(0.0, exit_row[f'sell_{side}_cents'] / 100 - p['slippage_per_leg_usd'])
                        proceeds = shares * (price_out - fee)
                        # A sell with nonpositive net proceeds is not assumed executable.
                        reconciled = proceeds > 0
                    if reconciled:
                        cash += proceeds
                        busy_until = exit_row['ts']
                        reason = 'hypothetical_roundtrip'
                    else:
                        halted = True
                        reason = 'unknown_exit_full_cost_loss_and_halt'
                    item.update({'side': side, 'shares': shares, 'entry_ts': entry['ts'], 'entry_cash_cost_usd': cost,
                                 'exit_ts': exit_row['ts'] if reconciled else None,
                                 'exit_cash_proceeds_usd': proceeds if reconciled else 0.0,
                                 'hypothetical_pnl_usd': (proceeds if reconciled else 0.0) - cost,
                                 'exit_is_quote_proxy': reconciled, 'minimum_order_rule_verified': False})
        reasons[reason] += 1
        peak = max(peak, cash)
        max_drawdown = max(max_drawdown, peak - cash)
        item.update({'status': reason, 'cash_after_usd': cash})
        ledger.append(item)
    return {'strategy': strategy, 'minimum_shares_assumed': shares, 'fee_per_share_per_leg_assumed': fee,
            'initial_cash_usd': p['initial_cash_usd'], 'ending_cash_lower_bound_usd': cash,
            'net_change_usd': cash - p['initial_cash_usd'], 'max_cash_drawdown_usd': max_drawdown,
            'terminal_unresolved_position': halted, 'status_counts': dict(reasons), 'attempt_ledger': ledger,
            'real_fills': 0, 'execution_evidence': False, 'cost_schedule_verified': False,
            'oos_confirmatory': False, 'cash_assumption': 'Hypothetical immediate cash credit only for a successful delayed exit quote; unknown exits halt.'}


def prior_coverage(trade_root, source_run_names):
    files, prior_runs = [], set()
    candidates = sorted(set((trade_root / 'reports').rglob('*coverage*.csv')) | set((trade_root / 'reports').rglob('*meta*.json')))
    for path in candidates:
        data = path.read_bytes()
        names = set(re.findall(r'\d{8,}_attempt\d+', data.decode('utf-8', errors='replace')))
        prior_runs.update(names)
        files.append({'path': str(path.relative_to(trade_root)), 'sha256': hashlib.sha256(data).hexdigest(),
                      'referenced_runs': sorted(names)})
    return {'files': files, 'source_runs_referenced_in_prior_coverage': sorted(prior_runs & source_run_names),
            'source_runs_not_found_in_this_limited_index': sorted(source_run_names - prior_runs),
            'warning': 'An absent reference is not proof of unseen data. Older source and docs already include extensive model/threshold selection. No pristine OOS claim.'}


def run(poly_root, trade_root, p):
    sources = {}
    for name, root, paths in (
        ('poly_raw_monthly', poly_root, sorted((poly_root / 'data/monthly_runs').rglob('*.csv'))),
        ('poly_other_archive', poly_root, sorted(x for x in (poly_root / 'data').rglob('*.csv') if 'monthly_runs' not in x.parts)),
        ('poly_trade_derived', trade_root, sorted((trade_root / 'data').rglob('*.csv'))),
    ):
        sources[name] = inventory(root, paths)
    dates = sources['poly_raw_monthly']['utc_window_dates']
    evaluation = dates[-p['evaluation_days']:]
    reference = dates[max(0, len(dates) - p['evaluation_days'] - p['reference_days']):-p['evaluation_days']]
    windows, dedup = load_selected(poly_root, sources['poly_raw_monthly']['files'], set(reference + evaluation))
    results = {}
    for split, split_dates in (('reference', reference), ('evaluation', evaluation)):
        selected = {slug: rows for slug, rows in windows.items() if day(int(slug.rsplit('-', 1)[1])) in split_dates}
        results[split] = {'utc_dates': split_dates, 'windows_after_conflict_exclusion': len(selected),
                         'simulations': [replay(selected, p, strategy, shares, fee)
                             for shares in p['minimum_shares_scenarios'] for fee in p['fee_per_share_per_leg_usd_scenarios']
                             for strategy in p['baselines']]}
    runs = {Path(f['path']).parts[2] for f in sources['poly_raw_monthly']['files']}
    references = ['analysis/build_run_24869603988_dataset.py', 'analysis/all_monthly_clob_systematic_research_v2.py',
                  'analysis/monthly_runs_walk_forward_validation.py', 'analysis/forward_roll_conflict.py',
                  'config/canary_forward_v1.json', 'docs/CANARY_READINESS.md']
    return {'schema_version': 1, 'protocol': p, 'source_commits': {'poly': revision(poly_root), 'poly_trade': revision(trade_root)},
            'source_inventory': sources, 'prior_exposure': prior_coverage(trade_root, runs),
            'selected_observation_audit': dedup, 'results': results,
            'source_references': [{'repo': 'poly_trade', 'path': f, 'sha256': sha256(trade_root / f)} for f in references]
                + [{'repo': 'poly', 'path': 'polymarket_quotes.py', 'sha256': sha256(poly_root / 'polymarket_quotes.py')}],
            'strict_verified_execution': {'status': 'blocked', 'qualifying_fills': 0,
                'missing_contract': ['per-side source event and receive time', 'contemporaneous fee schedule and rounding',
                                     'per-market minimum size and tick size', 'official outcome evidence for settlement-based tests',
                                     'unseen, precommitted forward windows and reliable coverage', 'private execution receipts']},
            'interpretation': ['This is a retrospective delayed quote sensitivity replay, not real trading or confirmatory OOS.',
                'A slug epoch is window start, not close; clean-table close timestamps are audited and ignored.',
                'The capture stamps seconds after sequential book GETs and before reference-price/trade calls: exact availability/side skew is unknown.',
                'No final_price/target_price/outcome_up value participates in a signal or payoff. No settlement payoff is inferred.',
                'Missing fees are not zero; both positive flat-cost scenarios are assumptions, not verified historical charges.',
                'The signal, dates, delays, share minimums and costs are fixed by the caller protocol; no winning configuration is selected.',
                'Existing archive data and prior results were already exposed; replay returns cannot certify unseen-data performance.'],
            'next_experiment': 'Freeze this baseline protocol before the next previously unseen v3 public capture; require timestamp/fee/minimum-size/tick provenance, measure one fixed forward block without retuning, and keep all execution claims blocked.'}


def write_private(path, result):
    path = Path(path).resolve()
    root = Path(__file__).resolve().parents[1]
    if path == root or root in path.parents:
        raise ValueError('PLAINTEXT_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(result, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--poly-root', type=Path, required=True)
    parser.add_argument('--trade-root', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        protocol_bytes = args.protocol.read_bytes()
        p = validate_protocol(json.loads(protocol_bytes))
        result = run(args.poly_root, args.trade_root, p)
        result['reproducibility'] = {'protocol_sha256': hashlib.sha256(protocol_bytes).hexdigest(),
            'code_sha256': sha256(Path(__file__)), 'python_version': sys.version,
            'command': 'python analysis/archive_baseline_audit.py --poly-root ../poly --trade-root . --protocol /PRIVATE/protocol.json --output /PRIVATE/result.json',
            'network_used': False, 'stdout_contains_results': False}
        write_private(args.output, result)
        print('LOCAL_AUDIT_COMPLETE. Seal the private output before publishing.')
    except Exception:
        print('AUDIT_BLOCKED. No empirical findings or private paths printed.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
