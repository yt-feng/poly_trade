"""Partial identification of recorded endpoint conditions, never order fills.

Read frozen decisions without fitting. Keep the first scheduled observations,
including invalid rows. Experiment settings and results belong in sealed output.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

try:
    import quote_measurement_audit as measurement
    from archive_coverage_ledger import connect_readonly, inventory, load_dates, verify_sources, utc_us
    from archive_baseline_audit import write_private
    from historical_walk_forward import file_hash
except ImportError:
    from analysis import quote_measurement_audit as measurement
    from analysis.archive_coverage_ledger import connect_readonly, inventory, load_dates, verify_sources, utc_us
    from analysis.archive_baseline_audit import write_private
    from analysis.historical_walk_forward import file_hash


VIEWS = ('full_book', 'own_field')
STATES = ('pass', 'fail', 'unknown')


def endpoint(row, index, allowed_time, view):
    """Only validate the named observed price; never synthesize the other side."""
    if view not in VIEWS:
        raise ValueError('unknown_measurement_view')
    if row is None or not allowed_time:
        return {'price': None, 'positive_size': False, 'reason': 'missing_or_outside_time_budget'}
    v = row['values']; i = index // 2 * 2
    reason = ('invalid_named_price' if not measurement.good_price(v[index]) else
              'observed_side_crossed' if all(measurement.good_price(v[j]) for j in (i, i+1)) and v[i+1] > v[i] else
              measurement.book_reason(v) if view == 'full_book' else None)
    return {'price': v[index] if reason is None else None,
            'positive_size': measurement.good_size(v[index+4]), 'reason': reason}


def condition(entry, exit_, buy_limit, sell_limit):
    """Missing/nonpositive size is unvalidated, not an invented failed order.

    These conservative bounds follow the frozen price-violation rule. A zero
    size is not certified absence of all liquidity, and cannot establish a pass.
    Decimal comparison avoids binary rounding changing an equality boundary.
    """
    buy, sell = Decimal(str(buy_limit)), Decimal(str(sell_limit))
    if not buy.is_finite() or not sell.is_finite() or not 0 < buy < 100 or sell < buy:
        raise ValueError('invalid_frozen_limit')
    failures = []
    if sell > 100:
        failures.append('sell_limit_above_binary_price_domain')
    if entry['price'] is not None and Decimal(str(entry['price'])) > buy:
        failures.append('entry_price_above_buy_limit')
    if exit_['price'] is not None and Decimal(str(exit_['price'])) < sell:
        failures.append('exit_price_below_sell_limit')
    unknown = []
    for role, value in (('entry', entry), ('exit', exit_)):
        if value['price'] is None:
            unknown.append(role+'_price_unvalidated:'+str(value['reason']))
        if not value['positive_size']:
            unknown.append(role+'_positive_size_unvalidated')
    return {'status': 'fail' if failures else 'unknown' if unknown else 'pass',
            'violations': failures, 'unvalidated': unknown}


def interval(passed, unknown, denominator):
    if min(passed, unknown, denominator) < 0 or passed+unknown > denominator:
        raise ValueError('invalid_bound_counts')
    return [passed/denominator, (passed+unknown)/denominator] if denominator else [None, None]


def finalize(counts):
    c = {k: counts.get(k, 0) for k in ('planned', 'eligible', 'selection_unavailable', 'not_selected',
                                      'selected', 'pass', 'fail', 'unknown')}
    c.update(counts)
    if (c['planned'] != c['eligible']+c['selection_unavailable'] or
            c['eligible'] != c['selected']+c['not_selected'] or
            c['selected'] != sum(c[s] for s in STATES)):
        raise ValueError('denominator_partition_mismatch')
    c['selected_condition_fraction_bounds'] = interval(c['pass'], c['unknown'], c['selected'])
    c['scheduled_selection_and_condition_bounds'] = interval(
        c['pass'], c['unknown']+c['selection_unavailable'], c['planned'])
    return c


def count_path(c, eligible, selected, event):
    c['planned'] += 1
    if not eligible:
        c['selection_unavailable'] += 1
        return
    c['eligible'] += 1
    if not selected:
        c['not_selected'] += 1
        return
    c['selected'] += 1; c[event['status']] += 1
    for label in event['violations']:
        c['violation:'+label] += 1
    for label in event['unvalidated']:
        c['unvalidated:'+label] += 1


def event_for(detail, side, view, allowance):
    i = 0 if side == 'up' else 2
    current, e, x = (detail['roles'][role] for role in ('current', 'entry', 'exit'))
    if current is None or not measurement.good_price(current['values'][i]):
        raise ValueError('missing_frozen_decision_price')
    buy = Decimal(str(current['values'][i])); sell = buy+Decimal(str(allowance))
    entry_time_ok = e is not None and detail['decision_us'] < measurement.stamp(e) < detail['target_us']
    exit_time_ok = detail['timing'] == 'record_within_tolerance'
    entry = endpoint(e, i, entry_time_ok, view)
    exit_ = endpoint(x, i+1, exit_time_ok, view)
    return {**condition(entry, exit_, buy, sell), 'buy_limit': str(buy), 'sell_limit': str(sell),
            'entry': entry, 'exit': exit_}


def run(database, root, frozen_file, protocol):
    repo = Path(__file__).resolve().parents[1]
    if file_hash(database) != protocol['database_sha256'] or file_hash(frozen_file) != protocol['frozen_report_sha256']:
        raise ValueError('frozen_input_changed')
    for path, sha in protocol['code_sha256'].items():
        if file_hash(repo/path) != sha:
            raise ValueError('frozen_code_changed')
    frozen = json.loads(Path(frozen_file).read_text())
    plan = frozen['protocol']; rows = frozen['window_ledger']; folds = frozen['folds']
    if plan != protocol['state_protocol'] or protocol['views'] != list(VIEWS):
        raise ValueError('frozen_definition_changed')
    prior_summary = frozen['summaries']; del frozen
    cfg = plan['prior_plan']['diagnostic']
    dates = sorted({d for f in plan['folds'] for d in f['validation_dates']})
    by_date = defaultdict(list)
    for row in rows:
        by_date[row['date']].append(row)
    if sorted(by_date) != dates:
        raise ValueError('validation_calendar_changed')
    names = ['state_'+n for n in plan['new_models']]+['legacy_'+n for n in cfg['models']]+['always_same_side','no_signal']
    panels = defaultdict(Counter); ledger = []; timing = defaultdict(Counter)
    with connect_readonly(database) as db:
        files = inventory(db); verify_sources(root, files)
        for date in dates:
            grouped, pc, dc, present = load_dates(db,[date])
            first = utc_us(date+'T00:00:00Z')//1000000
            expected = {(f'btc-updown-5m-{s}', o) for s in range(first, first+86400, 300) for o in plan['decision_offsets']}
            actual = {(r['market'], r['offset']) for r in by_date[date]}
            if actual != expected or len(actual) != len(by_date[date]):
                raise ValueError('missing_or_duplicate_frozen_slot')
            for r in by_date[date]:
                if (r['start'] != int(r['market'].split('-')[-1]) or r['date'] not in folds[r['fold']]['validation_dates']):
                    raise ValueError('market_or_fold_mismatch')
                detail = measurement.inspect_slot(grouped.get(r['market'], []), r['start'], r['offset'], cfg,
                    r['market'] in present, r['market'] in pc, r['market'] in dc)
                measurement.check_frozen(r, detail, cfg, folds[r['fold']]['new_fits'])
                if detail['crosses_window_end']:
                    raise ValueError('target_crosses_market_window')
                for role in ('current', 'entry', 'exit'):
                    point = detail['roles'][role]
                    delay = None if point is None else measurement.stamp(point)-(detail['target_us'] if role == 'exit' else detail['decision_us'])
                    timing[str(r['offset'])+'/'+role][str(delay) if delay is not None else 'missing'] += 1
                output = {'market':r['market'], 'date':date, 'offset':r['offset'], 'fold':r['fold'],
                    'eligible':r['eligible'], 'status':r['status'], 'signals':r['signals'],
                    'point_keys_us':{role:measurement.stamp(q) for role,q in detail['roles'].items()},
                    'points':{role:q['values'] if q else None for role,q in detail['roles'].items()}, 'sides':{}}
                for side,i in (('up',0),('down',2)):
                    current = detail['roles']['current']; bucket = measurement.price_bin(current['values'][i] if current else None)
                    output['sides'][side] = {'price_bin':bucket, 'events':{}}
                    for view in VIEWS:
                        event = event_for(detail, side, view, protocol['roundtrip_allowance_cents']) if r['eligible'] else None
                        output['sides'][side]['events'][view] = event
                        for name in names:
                            selected = bool(r['eligible'] and r['signals'][side][name])
                            for axis,value in (('total','all'),('date',date),('fold',str(r['fold'])),('price_bin',bucket),('date_price_bin',date+'/'+bucket)):
                                count_path(panels[f'{r["offset"]}/{side}/{view}/{name}/{axis}/{value}'],r['eligible'],selected,event)
                ledger.append(output)
        verify_sources(root, files)
    # Materialize empty date/price cells. Empty cells have null fractions, not 0%.
    bins = ['0:20','20:40','40:60','60:80','80:100','unavailable']
    for offset in plan['decision_offsets']:
        for side in ('up','down'):
            for view in VIEWS:
                for name in names:
                    prefix = f'{offset}/{side}/{view}/{name}/'
                    for bucket in bins:
                        panels[prefix+'price_bin/'+bucket]
                        for date in dates:
                            panels[prefix+'date_price_bin/'+date+'/'+bucket]
    result_panels = {k:finalize(v) for k,v in panels.items()}
    for offset in plan['decision_offsets']:
        for side in ('up','down'):
            for view in VIEWS:
                for name in names:
                    prefix = f'{offset}/{side}/{view}/{name}/'
                    total = result_panels[prefix+'total/all']
                    for axis in ('date','price_bin','date_price_bin','fold'):
                        parts = [v for k,v in result_panels.items() if k.startswith(prefix+axis+'/')]
                        for field in ('planned','eligible','selected','pass','fail','unknown','selection_unavailable'):
                            if sum(p[field] for p in parts) != total[field]:
                                raise ValueError('panel_partition_mismatch')
    prior = {side:{offset:{name:value for name,value in views['positive_depth']['1']['assumed_fee']['policies'].items()}
                          for offset,views in offsets.items()} for side,offsets in prior_summary.items()}
    return {'protocol':protocol,'raw_inventory':files,'panels':result_panels,'window_ledger':ledger,
        'timing_microseconds_histograms':dict(timing),'prior_frozen_quote_summaries':prior,
        'checked_frozen_slots':len(ledger),'frozen_mismatches':0,'pnl':None,'promotion_allowed':False,
        'canary_gate_changed':False,'real_fills':0,'fill_probability_lower_bound':0,
        'scope':'exposed-history recorded endpoint conditions, no refit or trade simulation',
        'limits':['No missing-at-random assumption for observation-level bounds.',
          'Invalid sizes remain unvalidated; intervals are conservative and not necessarily sharp.',
          'Fractions are neither confidence intervals nor continuous-path or order-fill bounds.',
          'Unknown candidate selection is retained separately on the entire scheduled calendar.',
          'All source and receive timing, queue, fee and minimum-order restrictions remain unverified.']}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('database','poly-root','frozen-report','protocol','output'):
        p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args(argv)
    result = run(a.database, a.poly_root, a.frozen_report, json.loads(a.protocol.read_text()))
    result['protocol_sha256'] = file_hash(a.protocol)
    write_private(a.output, result)
    print('PRIVATE_RECORDED_CONDITION_BOUND_COMPLETE; no empirical contents printed.')


if __name__ == '__main__':
    main()
