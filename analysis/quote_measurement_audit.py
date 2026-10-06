"""Audit frozen quote-label measurements without fitting or evaluating a strategy.

Raw strings, source lines and missing observations stay in private output. A
side-only availability count is a diagnostic, never a replacement trading label.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path

try:
    from archive_coverage_ledger import connect_readonly, inventory, load_dates, utc_us, verify_sources
    from archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS, number, market_start, write_private
    from historical_walk_forward import file_hash
except ImportError:
    from analysis.archive_coverage_ledger import connect_readonly, inventory, load_dates, utc_us, verify_sources
    from analysis.archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS, number, market_start, write_private
    from analysis.historical_walk_forward import file_hash


def good_price(x):
    return x is not None and math.isfinite(x) and 0 < x < 100


def good_size(x):
    return x is not None and math.isfinite(x) and x > 0


def book_reason(values):
    prices = values[:4]
    if any(x is None or not math.isfinite(x) for x in prices):
        return 'missing_or_nonfinite_price'
    if not all(good_price(x) for x in prices):
        return 'price_outside_binary_range'
    if prices[1] > prices[0] or prices[3] > prices[2]:
        return 'crossed_price'
    return None


def size_reason(values):
    sizes = values[4:]
    if any(x is None or not math.isfinite(x) for x in sizes):
        return 'missing_or_nonfinite_depth'
    return 'nonpositive_depth' if not all(good_size(x) for x in sizes) else None


def stamp(row):
    return None if row is None else round(row['ts'] * 1000000)


def pick(rows, index):
    return rows[index] if 0 <= index < len(rows) else None


def inspect_slot(rows, start, offset, cfg, present=True, price_conflict=False, depth_conflict=False):
    """Independent integer-UTC matching; preserve the first invalid row."""
    times = [stamp(r) for r in rows]
    if times != sorted(set(times)) or any(not start*1000000 <= t < (start+300)*1000000 for t in times):
        raise ValueError('duplicate_or_cross_window_record')
    decision = (start + offset)*1000000
    cutoff = decision - cfg['lookback_seconds']*1000000
    target = decision + cfg['horizon_seconds']*1000000
    current = pick(rows, bisect.bisect_left(times, decision)-1)
    lag = pick(rows, bisect.bisect_right(times, cutoff)-1)
    entry = pick(rows, bisect.bisect_right(times, decision))
    future_index = bisect.bisect_left(times, target)
    future = pick(rows, future_index)
    timing = ('no_record_at_or_after_target' if future is None else
              'first_record_after_tolerance' if stamp(future) > target+cfg['future_tolerance_seconds']*1000000 else
              'record_within_tolerance')
    status = ('excluded_price_conflict' if price_conflict else 'excluded_depth_conflict' if depth_conflict else
              ('present_invalid_time_only' if present else 'absent_window') if not rows else None)
    if status is None:
        for role, row, end in (('decision', current, decision), ('lookback', lag, cutoff)):
            if row is None or end-stamp(row) > cfg['max_quote_age_seconds']*1000000:
                status = 'excluded_'+role+'_missing_or_stale'
            elif book_reason(row['values']):
                status = 'excluded_'+role+'_price'
            elif size_reason(row['values']):
                status = 'excluded_'+role+'_depth'
            if status:
                break
    feature = status is None
    if feature:
        status = ('unknown_future_missing' if timing != 'record_within_tolerance' else
                  'unknown_future_price' if book_reason(future['values']) else 'labeled_quote_change')
    detail = {
        'feature_valid': feature, 'status': status, 'decision_us': decision, 'target_us': target,
        'crosses_window_end': target+cfg['future_tolerance_seconds']*1000000 >= (start+300)*1000000,
        'timing': timing, 'roles': {'current': current, 'lag': lag, 'entry': entry, 'exit': future},
        'first_record_us': times[0] if times else None, 'last_record_us': times[-1] if times else None,
        'last_before_target_us': times[future_index-1] if future_index else None,
        'exit_has_later_records': bool(future is not None and future_index+1 < len(rows)),
        'exit_row_reason': book_reason(future['values']) if future else 'no_record',
        'side': {},
    }
    for side, i in (('up', 0), ('down', 2)):
        within = timing == 'record_within_tolerance'
        bid_ok = within and good_price(future['values'][i+1])
        side_book_ok = bid_ok and good_price(future['values'][i]) and future['values'][i+1] <= future['values'][i]
        whole_ok = within and book_reason(future['values']) is None
        if not within:
            cause = timing
        elif not good_price(future['values'][i+1]):
            cause = 'own_bid_missing_or_invalid'
        elif not good_price(future['values'][i]) or future['values'][i+1] > future['values'][i]:
            cause = 'own_ask_invalid_or_side_crossed'
        elif not whole_ok:
            cause = 'opposite_book_invalid'
        else:
            cause = 'complete_quote_label'
        recovery = next((r for r in rows[future_index+1:] if good_price(r['values'][i+1])), None) if future else None
        paths = {}
        for step, e in (('0', current), ('1', entry)):
            entry_ok = e is not None and stamp(e) < target and book_reason(e['values']) is None
            price_known = entry_ok and whole_ok
            depth_ok = all(r is not None and all(good_size(r['values'][j]) for j in indices)
                           for r, indices in ((current, (i+4, i+5)), (e, (i+4,)), (future, (i+5,))))
            own_entry_ok = e is not None and stamp(e) < target and good_price(e['values'][i])
            paths[step] = {'price_only_known': bool(price_known), 'positive_depth_known': bool(price_known and depth_ok),
                'side_only_price_available': bool(own_entry_ok and bid_ok),
                'side_only_positive_depth_available': bool(own_entry_ok and bid_ok and depth_ok),
                'entry_before_exit': e is not None and stamp(e) < target,
                'entry_row_reason': book_reason(e['values']) if e else 'no_record',
                'entry_ask_field_valid': e is not None and good_price(e['values'][i])}
        detail['side'][side] = {'exit_cause_disjoint': cause, 'exit_bid_field_valid_in_time': bool(bid_ok),
            'exit_side_book_valid_in_time': bool(side_book_ok), 'exit_whole_book_valid_in_time': bool(whole_ok),
            'exit_bid_positive_depth_in_time': bool(bid_ok and good_size(future['values'][i+5])),
            'exit_own_bid_later_recovers': bool(within and not bid_ok and recovery),
            'first_later_valid_bid_us': stamp(recovery) if within and not bid_ok else None, 'paths': paths}
    return detail


def check_frozen(frozen, detail, cfg, fits):
    """Reject join, label, feature, prediction and gate mismatches; never refit."""
    def check(ok, label):
        if not ok:
            raise ValueError('frozen_measurement_mismatch:'+label)
    check(frozen['feature_eligible'] == detail['feature_valid'] and frozen['status'] == detail['status'], 'eligibility')
    if not detail['feature_valid']:
        check(not frozen['eligible'] and not frozen['signals'], 'ineligible_signal')
        return
    cur, lag, future = (detail['roles'][x] for x in ('current', 'lag', 'exit'))
    check([lag['ts'], cur['ts']] == frozen['feature_times'], 'feature_times')
    check(frozen['observed_exit_time'] == (future['ts'] if future else None), 'first_exit')
    label = detail['status'] == 'labeled_quote_change'
    check(frozen['target_time'] == (future['ts'] if label else None), 'target_time')
    for side, i in (('up', 0), ('down', 2)):
        ask, bid = cur['values'][i:i+2]
        check(frozen['current_prices'][side] == {'ask': ask, 'bid': bid}, 'current_prices')
        expected_targets = {name: future['values'][j]-cur['values'][j]
                            for name, j in (('bid_change_cents', i+1), ('ask_change_cents', i))} if label else None
        check(frozen['side_targets'][side] == expected_targets, 'labels')
        p = (ask+bid)/200; clock = cfg['horizon_seconds']/(300-frozen['offset'])
        momentum = (ask+bid-lag['values'][i]-lag['values'][i+1])/2
        features = {'spread': ask-bid, 'level': 2*p-1, 'level_clock': (2*p-1)*clock,
                    'momentum_boundary_clock': momentum*4*p*(1-p)*clock}
        check(features == frozen['state_features'][side], 'state_features')
        for step, path in detail['side'][side]['paths'].items():
            before = frozen['paths'][side][step]
            e = detail['roles']['current' if step == '0' else 'entry']
            entry_ok = e is not None and stamp(e) < detail['target_us'] and book_reason(e['values']) is None
            check(before['entry_time'] == (e['ts'] if e else None), 'entry_time')
            check(before['ask'] == (e['values'][i] if entry_ok else None), 'entry_ask')
            check(before['bid'] == (future['values'][i+1] if label else None), 'exit_bid')
            for view in ('price_only', 'positive_depth'):
                check(before[view]['known'] == path[view+'_known'], 'path_known')
        if not frozen['eligible']:
            continue
        for target, models in fits[side].items():
            j = i+1 if target == 'bid_change_cents' else i
            for name, model in models.items():
                raw = model['intercept'] + sum((features[f]-c)/s*b for f,c,s,b in
                    zip(model['fields'], model['center'], model['scale'], model['coefficients']))
                raw = min(100, max(-100, raw))
                projected = min(100-cur['values'][j], max(-cur['values'][j], raw))
                check(math.isclose(frozen['raw_predictions'][side][target]['state_'+name], raw, abs_tol=1e-12), 'raw_prediction')
                check(math.isclose(frozen['predictions'][side][target]['state_'+name], projected, abs_tol=1e-12), 'prediction')
        for name, prediction in frozen['predictions'][side]['bid_change_cents'].items():
            check(frozen['signals'][side][name] == (prediction > ask-bid+(1 if name.startswith('state_') else 0)), 'signal')
        check(frozen['signals'][side]['always_same_side'] and not frozen['signals'][side]['no_signal'], 'control_gate')


def price_bin(value):
    return f'{min(4, int(value//20))*20}:{min(4, int(value//20))*20+20}' if good_price(value) else 'unavailable'


def update_counts(counts, frozen, detail, side, selected):
    counts['planned'] += 1
    counts['feature_valid'] += int(detail['feature_valid'])
    counts['prediction_available'] += int(frozen['eligible'])
    counts['selected'] += int(selected)
    s = detail['side'][side]
    for prefix, included in (('all_feature_valid', detail['feature_valid']), ('selected', selected)):
        if not included:
            continue
        counts[prefix+'_exit_cause:'+s['exit_cause_disjoint']] += 1
        for field in ('exit_bid_field_valid_in_time','exit_side_book_valid_in_time','exit_whole_book_valid_in_time',
                      'exit_bid_positive_depth_in_time','exit_own_bid_later_recovers'):
            counts[prefix+'_'+field] += int(s[field])
        for step, path in s['paths'].items():
            for field in ('price_only_known','positive_depth_known','side_only_price_available','side_only_positive_depth_available'):
                counts[prefix+'_entry'+step+'_'+field] += int(path[field])


def raw_field_class(value):
    if value is None or not str(value).strip():
        return 'empty'
    parsed = number(value)
    if parsed is None:
        return 'malformed_or_nonfinite'
    return 'zero' if parsed == 0 else 'negative' if parsed < 0 else 'positive'


def scan_raw(root, files, wanted):
    """Verify every requested raw key; keep exact CSV cells and file boundaries."""
    fields = PRICE_FIELDS+SIZE_FIELDS+('level_count_ask_up','level_count_bid_up','level_count_ask_down','level_count_bid_down')
    found = {}; windows = {}; raw_counts = Counter(); bins = defaultdict(Counter); boundaries = []
    for info in files:
        raw = (Path(root)/info['path']).read_bytes()
        import hashlib
        if len(raw) != info['bytes'] or hashlib.sha256(raw).hexdigest() != info['sha256']:
            raise ValueError('source_bytes_changed')
        first = last = None; first_by_slug = {}; last_by_slug = {}
        for line, row in enumerate(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))), 2):
            raw_counts['rows'] += 1
            slug, start = market_start(row); ts = utc_us(row.get('ts_iso'))
            if ts is None or start is None:
                raw_counts['bad_identity_or_timestamp'] += 1
                continue
            first = ts if first is None else min(first, ts); last = ts if last is None else max(last, ts)
            first_by_slug[slug] = min(first_by_slug.get(slug,ts),ts); last_by_slug[slug] = max(last_by_slug.get(slug,ts),ts)
            if not start*1000000 <= ts < (start+300)*1000000:
                raw_counts['outside_window'] += 1
                continue
            raw_counts['valid_time_rows'] += 1
            b = bins[str((ts//1000000-start)//30*30)]; b['rows'] += 1
            for field in PRICE_FIELDS+SIZE_FIELDS:
                b[field+':'+raw_field_class(row.get(field))] += 1
            key = (slug,ts)
            if key in wanted:
                values = [number(row.get(f)) for f in PRICE_FIELDS+SIZE_FIELDS]
                if values != list(wanted[key]):
                    raise ValueError('raw_database_values_mismatch')
                cells = {f:row.get(f) for f in fields}
                ref = {'file':info['path'],'csv_line':line,'file_sha256':info['sha256']}
                if key in found:
                    if cells != found[key]['cells']:
                        raise ValueError('raw_measurement_conflict')
                    found[key]['sources'].append(ref)
                else:
                    found[key] = {'cells':cells,'sources':[ref]}
        boundaries.append({'file':info['path'],'first_us':first,'last_us':last})
        for slug in first_by_slug:
            w = windows.setdefault(slug,[])
            w.append({'file':info['path'],'first_us':first_by_slug[slug],'last_us':last_by_slug[slug],
                      'file_first_us':first,'file_last_us':last})
    if set(found) != set(wanted):
        raise ValueError('raw_requested_records_missing')
    return {'counts':dict(raw_counts),'relative_30second_field_counts':dict(bins),'file_boundaries':boundaries}, found, windows


def run(database, root, frozen_file, protocol):
    if file_hash(database) != protocol['database_sha256'] or file_hash(frozen_file) != protocol['frozen_report_sha256']:
        raise ValueError('frozen_input_changed')
    repo = Path(__file__).resolve().parents[1]
    for name, sha in protocol['code_sha256'].items():
        if file_hash(repo/name) != sha:
            raise ValueError('frozen_code_changed')
    frozen = json.loads(Path(frozen_file).read_text()); plan = frozen['protocol']
    if plan != protocol['state_protocol']:
        raise ValueError('state_protocol_changed')
    rows = frozen.pop('window_ledger'); folds = frozen['folds']; del frozen
    by_date = defaultdict(list)
    for row in rows:
        by_date[row['date']].append(row)
    cfg = plan['prior_plan']['diagnostic']; dates = sorted({d for f in plan['folds'] for d in f['validation_dates']})
    if sorted(by_date) != dates:
        raise ValueError('frozen_validation_dates_changed')
    panels = defaultdict(Counter); slot_counts = defaultdict(Counter); output = []; wanted = {}; seen = set()
    with connect_readonly(database) as db:
        files = inventory(db); verify_sources(root, files)
        for date in dates:
            grouped, pc, dc, present = load_dates(db,[date])
            first = utc_us(date+'T00:00:00Z')//1000000
            expected = {(f'btc-updown-5m-{s}',o) for s in range(first,first+86400,300) for o in plan['decision_offsets']}
            actual = {(r['market'],r['offset']) for r in by_date[date]}
            if actual != expected or len(actual) != len(by_date[date]):
                raise ValueError('missing_or_duplicate_planned_slot')
            for r in by_date[date]:
                key = (r['market'],r['offset'])
                if key in seen:
                    raise ValueError('repeated_slot')
                seen.add(key)
                if r['start'] != int(r['market'].split('-')[-1]) or r['date'] not in folds[r['fold']]['validation_dates']:
                    raise ValueError('market_or_fold_mismatch')
                detail = inspect_slot(grouped.get(r['market'],[]),r['start'],r['offset'],cfg,
                                      r['market'] in present,r['market'] in pc,r['market'] in dc)
                check_frozen(r,detail,cfg,folds[r['fold']]['new_fits'])
                count = slot_counts[str(r['offset'])]; count['planned'] += 1; count[detail['status']] += 1
                count['crosses_window_end'] += int(detail['crosses_window_end'])
                names = ['state_'+n for n in plan['new_models']]+['legacy_'+n for n in plan['prior_plan']['diagnostic']['models']]+['always_same_side','no_signal']
                for side,i in (('up',0),('down',2)):
                    current = detail['roles']['current']; ask = current['values'][i] if current else None
                    bucket = price_bin(ask)
                    for name in names:
                        selected = bool(r['eligible'] and r['signals'][side][name])
                        for axis,value in (('total','all'),('date',date),('price_bin',bucket),('date_price_bin',date+'/'+bucket)):
                            update_counts(panels[f'{r["offset"]}/{side}/{name}/{axis}/{value}'],r,detail,side,selected)
                for role,q in detail['roles'].items():
                    if q is not None:
                        wanted[(r['market'],stamp(q))] = q['values']
                    detail['roles'][role] = stamp(q)
                output.append({'market':r['market'],'date':date,'offset':r['offset'],'fold':r['fold'],
                    'signals':r['signals'],'eligible':r['eligible'],'measurement':detail})
        raw, points, windows = scan_raw(root,files,wanted)
        verify_sources(root,files)
    evidence = [{'market':m,'ts_us':t,**value} for (m,t),value in sorted(points.items())]
    for row in output:
        d = row['measurement']; spans = windows.get(row['market'],[])
        d['market_file_spans'] = spans
        d['same_source_file_has_later_market_rows'] = bool(spans and any(s['file_last_us']>d['last_record_us'] for s in spans)) if d['last_record_us'] else False
        d['market_last_is_a_file_end'] = bool(spans and any(s['last_us']==s['file_last_us']==d['last_record_us'] for s in spans))
    for c in panels.values():
        if not c['planned'] >= c['feature_valid'] >= c['prediction_available'] >= c['selected'] >= c['selected_entry1_positive_depth_known']:
            raise ValueError('non_nested_denominator')
    return {'scope':'frozen historical measurement audit; no refit, changed labels or strategy evaluation',
        'protocol':protocol,'slot_counts':dict(slot_counts),'panels':dict(panels),'raw_inventory':raw,
        'point_evidence':evidence,'window_ledger':output,'checked_frozen_slots':len(output),
        'measurement_mismatches':0,'pnl':None,'promotion_allowed':False,'canary_gate_changed':False,
        'caveats':['A file ending or an empty side does not identify its cause.',
                   'Side-only counts diagnose the complete-book rule; they do not replace frozen labels or prove execution.',
                   'Local timestamp ordering does not authenticate atomic books, source times, venue closure or fills.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('database','poly-root','frozen-report','protocol','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    a = parser.parse_args(argv)
    result = run(a.database,a.poly_root,a.frozen_report,json.loads(a.protocol.read_text()))
    result['protocol_sha256'] = file_hash(a.protocol)
    write_private(a.output,result)
    print('PRIVATE_MEASUREMENT_AUDIT_COMPLETE; no empirical contents printed.')


if __name__ == '__main__':
    main()
