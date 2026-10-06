"""Falsify frozen quote proxies without refitting, reselection or execution claims."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics

try:
    import spread_hurdle_diagnostic as hurdle
    from archive_coverage_ledger import connect_readonly, inventory, verify_sources
    from archive_baseline_audit import write_private
    from historical_walk_forward import file_hash
    from quote_factor_diagnostic import predict
except ImportError:
    from analysis import spread_hurdle_diagnostic as hurdle
    from analysis.archive_coverage_ledger import connect_readonly, inventory, verify_sources
    from analysis.archive_baseline_audit import write_private
    from analysis.historical_walk_forward import file_hash
    from analysis.quote_factor_diagnostic import predict


def validate_plan(p):
    required = {'entry_steps': [0, 1, 3], 'block_hours': 6, 'top_counts': [1, 3, 5],
                'exit_policy': 'original_scheduled_exit_first_record_no_shift',
                'selection': 'all_prior_candidates_no_refit_or_reselection',
                'quantiles': [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99],
                'views': ['price_only', 'positive_depth'],
                'step_origin': 'strictly_after_decision_actual_records_no_invalid_skip'}
    if any(p.get(k) != v for k, v in required.items()):
        raise ValueError('unregistered_robustness_rule')
    hurdle.validate_plan(p['prior_hurdle_protocol'])


def raw_quote(db, market, clause, value, order='ASC', limit=1):
    if clause not in ('=', '<', '<=', '>', '>=') or order not in ('ASC', 'DESC'):
        raise ValueError('invalid_query')
    rows = db.execute(f"""SELECT ts_us,start,prices,sizes,price_reason,depth_reason,
          repeats,price_conflict,depth_conflict FROM observations
          WHERE slug=? AND ts_us {clause} ? ORDER BY ts_us {order} LIMIT ?""",
          (market, round(value * 1000000), limit)).fetchall()
    return [dict(zip(('ts_us','start','prices','sizes','price_reason','depth_reason',
                     'repeats','price_conflict','depth_conflict'), row)) |
            {'prices': json.loads(row[2]), 'sizes': json.loads(row[3])} for row in rows]


def check_raw(q, start):
    if q['start'] != start or not start * 1000000 <= q['ts_us'] < (start + 300) * 1000000:
        raise ValueError('cross_window_or_invalid_quote_index')
    if len(q['prices']) != 4 or len(q['sizes']) != 4:
        raise ValueError('invalid_book_shape')


def price_issue(q):
    if q is None:
        return 'missing_record'
    if q['price_conflict']:
        return 'duplicate_price_conflict'
    prices = q['prices']
    if any(type(x) not in (float, int) or not math.isfinite(x) for x in prices):
        return 'missing_or_nonfinite_price'
    if any(not 0 < x < 100 for x in prices):
        return 'price_outside_range'
    if prices[1] > prices[0] or prices[3] > prices[2]:
        return 'crossed_spread'
    return q['price_reason']


def depth_issue(q, indexes):
    if q is None:
        return 'missing_record'
    if q['depth_conflict']:
        return 'duplicate_depth_conflict'
    sizes = [q['sizes'][i] for i in indexes]
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in sizes):
        return 'missing_or_nonfinite_depth'
    return 'nonpositive_depth' if any(x <= 0 for x in sizes) else None


def attach(db, original, prior_path, cfg):
    # Independently reproduce all old prices/features/targets before new stresses.
    baseline = hurdle.attach_path(db, original, cfg)
    if baseline != prior_path:
        raise ValueError('prior_quote_path_changed')
    start = original['start']
    if (original['market'] != f'btc-updown-5m-{start}' or start % 300 or
            original['date'] != datetime.fromtimestamp(start, timezone.utc).date().isoformat() or
            original['decision_time'] != start + cfg['decision_offset_seconds']):
        raise ValueError('invalid_decision_identity_or_index')
    result = {'market': original['market'], 'date': original['date'], 'start': start,
              'fold': original['fold'], 'status': original['status'], 'eligible': baseline['eligible'],
              'signals': {}, 'paths': {}}
    if not baseline['eligible']:
        return result
    market, decision = original['market'], original['decision_time']
    current = raw_quote(db, market, '<', decision, 'DESC')
    lag = raw_quote(db, market, '<=', decision - cfg['lookback_seconds'], 'DESC')
    if not current or not lag or [lag[0]['ts_us'] / 1e6, current[0]['ts_us'] / 1e6] != original['feature_times']:
        raise ValueError('decision_or_lookback_not_last_causal_record')
    current = current[0]
    check_raw(current, start); check_raw(lag[0], start)
    if price_issue(current) or price_issue(lag[0]):
        raise ValueError('invalid_frozen_decision_book')
    scheduled_exit = decision + cfg['horizon_seconds']
    if not start < decision < scheduled_exit < start + 300:
        raise ValueError('invalid_scheduled_exit')
    raw_exit = raw_quote(db, market, '>=', scheduled_exit)
    exit_quote = raw_exit[0] if raw_exit else None
    if exit_quote is not None:
        check_raw(exit_quote, start)
    exit_reason = ('exit_missing_or_late' if exit_quote is None or
                   exit_quote['ts_us'] > (scheduled_exit + cfg['future_tolerance_seconds']) * 1e6 else
                   price_issue(exit_quote))
    exit_ok = exit_reason is None
    if exit_ok != (original['targets'] is not None):
        raise ValueError('label_validity_index_mismatch')
    later = raw_quote(db, market, '>', decision, limit=3)
    for q in later:
        check_raw(q, start)
    result.update(decision_time=decision, scheduled_exit_time=scheduled_exit,
                  fixed_exit_record=exit_quote, decision_record=current)
    names = list(original['predictions']['bid_change_cents']) + ['always_same_side', 'no_signal']
    for side, i in (('up', 0), ('down', 2)):
        result['signals'][side] = {name: hurdle.signal(baseline, name, side) for name in names}
        result['paths'][side] = {}
        for step in (0, 1, 3):
            entry = current if step == 0 else later[step - 1] if len(later) >= step else None
            entry_reason = ('entry_missing' if entry is None else
                            'entry_at_or_after_fixed_exit' if entry['ts_us'] >= scheduled_exit * 1e6 else price_issue(entry))
            if step and entry is not None and entry['ts_us'] <= decision * 1e6:
                raise ValueError('delayed_entry_not_after_decision')
            a = entry['prices'][i] if entry_reason is None else None
            b = exit_quote['prices'][i + 1] if exit_ok else None
            reasons = [str(r) for r in (entry_reason, exit_reason) if r]
            strict_reasons = list(reasons)
            for role, q, indices in (('decision', current, [i, i + 1]), ('entry', entry, [i]),
                                     ('exit', exit_quote, [i + 1])):
                issue = depth_issue(q, indices)
                if issue:
                    strict_reasons.append(role + '_' + issue)
            result['paths'][side][str(step)] = {
                'entry_record': entry, 'entry_delay_recorded_seconds': None if entry is None else entry['ts_us'] / 1e6 - decision,
                'ask': a, 'bid': b, 'price_only': {'known': not reasons, 'unknown_reasons': reasons},
                'positive_depth': {'known': not strict_reasons, 'unknown_reasons': strict_reasons}}
    return result


def distribution(values):
    s = sorted(values)
    if not s:
        return {'n': 0, 'mean': None, 'median': None, 'quantiles_nearest_rank': {}, 'min': None, 'max': None}
    return {'n': len(s), 'mean': statistics.mean(s), 'median': statistics.median(s),
            'quantiles_nearest_rank': {str(q): s[max(0, math.ceil(q * len(s)) - 1)] for q in (.01,.05,.25,.5,.75,.95,.99)},
            'min': s[0], 'max': s[-1]}


def concentration(values):
    net, absolute, positive = sum(values), sum(abs(v) for v in values), sum(max(v, 0) for v in values)
    winners, magnitudes = sorted((v for v in values if v > 0), reverse=True), sorted(map(abs, values), reverse=True)
    return {'net_sum': net, 'gross_positive_sum': positive, 'gross_negative_sum': sum(min(v,0) for v in values),
            'absolute_sum': absolute, 'top': {str(k): {
                'positive_share_of_gross_positive': sum(winners[:k]) / positive if positive else None,
                'positive_to_net_ratio': sum(winners[:k]) / net if net > 0 else None,
                'absolute_share': sum(magnitudes[:k]) / absolute if absolute else None,
                'remaining_mean_after_removing_largest_positive': (net - sum(winners[:k])) / (len(values) - min(k,len(winners)))
                    if len(values) > min(k,len(winners)) else None}
                for k in (1, 3, 5)}}


def bounds(path, view, cost):
    if path[view]['known']:
        v = path['bid'] - path['ask'] - cost
        return v, v
    # A depth failure makes the quote proxy unavailable in that view; don't
    # silently condition on the still displayed price as executable evidence.
    if view == 'positive_depth':
        return -100 - cost, 100 - cost
    ask_low, ask_high = (path['ask'], path['ask']) if path['ask'] is not None else (0, 100)
    bid_low, bid_high = (path['bid'], path['bid']) if path['bid'] is not None else (0, 100)
    return bid_low - ask_high - cost, bid_high - ask_low - cost


def leave_groups(groups, total, count, known_count, low, high, selected):
    details = {}
    for key, g in sorted(groups.items()):
        n = count - g['n']
        k = known_count - g['known']
        selected_left = selected - g['selected']
        details[key] = {'removed_observed_selected': g['n'], 'removed_selected_unknown': g['selected'] - g['n'],
            'removed_contribution': g['total'], 'retained_selected_observed': n,
            'retained_selected_mean': (total - g['total']) / n if n else None,
            'retained_shared_activity_mean': (total - g['total']) / k if k else None,
            'retained_selected_lower': (low - g['low']) / selected_left if selected_left else None,
            'retained_selected_upper': (high - g['high']) / selected_left if selected_left else None}
    means = [x['retained_selected_mean'] for x in details.values() if x['retained_selected_mean'] is not None]
    return {'groups': details, 'min_retained_mean': min(means) if means else None,
            'max_retained_mean': max(means) if means else None,
            'nonpositive_retained_groups': sum(v <= 0 for v in means),
            'groups_with_observed_selected': sum(g['n'] > 0 for g in groups.values()),
            'nonpositive_retained_after_removing_active_group': sum(x['removed_observed_selected'] > 0 and x['retained_selected_mean'] is not None and x['retained_selected_mean'] <= 0 for x in details.values()),
            'contribution_concentration': concentration([g['total'] for g in groups.values()])}


def summarize(rows, names, side, step, view, scenario):
    cost = 2 * (scenario['fee_cents_per_share_per_leg'] + scenario['slippage_cents_per_share_per_leg'])
    eligible = [r for r in rows if r['eligible']]
    known = [r for r in eligible if r['paths'][side][step][view]['known']]
    shared_delays = [r for r in eligible if all(p[view]['known'] for p in r['paths'][side].values())]
    shared_markets = {r['market'] for r in shared_delays}
    unknown_reasons = Counter(reason for r in eligible for reason in r['paths'][side][step][view]['unknown_reasons'])
    result = {'scheduled': len(rows), 'eligible': len(eligible), 'unobservable_signal': len(rows)-len(eligible),
              'known': len(known), 'unknown': len(eligible)-len(known), 'unknown_reasons_overlap': dict(unknown_reasons),
              'shared_all_delay_known': len(shared_delays), 'cost_assumption': scenario, 'policies': {},
              'recorded_entry_delay_seconds': distribution([r['paths'][side][step]['entry_delay_recorded_seconds']
                   for r in eligible if r['paths'][side][step]['entry_delay_recorded_seconds'] is not None])}
    day_keys = sorted({r['date'] for r in rows})
    block_keys = sorted({str(r['start'] // 21600 * 21600) for r in rows}, key=int)
    for name in list(names) + ['always_same_side', 'no_signal']:
        margins, paired = [], {b: 0.0 for b in ('always_same_side','no_signal')}
        selected = common_selected = 0; low = high = common_total = base_common_total = 0.0
        selected_reasons = Counter()
        groups = {kind: {key: Counter() for key in keys} for kind, keys in (('day',day_keys),('six_hour',block_keys))}
        for r in eligible:
            path = r['paths'][side][step]
            active = r['signals'][side][name]
            good = path[view]['known']
            v = path['bid'] - path['ask'] - cost if good else None
            selected += active
            lo, hi = bounds(path, view, cost)
            lo *= active; hi *= active
            low += lo; high += hi
            if good:
                for baseline in paired:
                    paired[baseline] += (int(active)-int(r['signals'][side][baseline]))*v
                if active:
                    margins.append(v)
            elif active:
                selected_reasons.update(path[view]['unknown_reasons'])
            if r['market'] in shared_markets and active:
                common_total += v
                common_selected += 1
                base = r['paths'][side]['0']
                base_common_total += base['bid'] - base['ask'] - cost
            for kind,key in (('day',r['date']),('six_hour',str(r['start']//21600*21600))):
                g = groups[kind][key]
                g.update(n=int(active and good), selected=int(active), known=int(good),
                         total=v if active and good else 0, low=lo, high=hi)
        total = sum(margins)
        result['policies'][name] = {
            'selected': selected, 'observed_selected': len(margins), 'unknown_selected': selected-len(margins),
            'selected_unknown_reasons_overlap': dict(selected_reasons),
            'observed_distribution': distribution(margins), 'window_concentration': concentration(margins),
            'selected_lower': low/selected if selected else None, 'selected_upper': high/selected if selected else None,
            'shared_activity_mean': total/len(known) if known else None,
            'paired_same_sample_vs': {b: v/len(known) if known else None for b,v in paired.items()},
            'all_delay_same_sample_selected': common_selected,
            'all_delay_same_sample_selected_mean': common_total/common_selected if common_selected else None,
            'all_delay_same_sample_selected_delta_vs_original': (common_total-base_common_total)/common_selected if common_selected else None,
            'all_delay_same_sample_activity_mean': common_total/len(shared_delays) if shared_delays else None,
            'all_delay_same_sample_delta_vs_original': (common_total-base_common_total)/len(shared_delays) if shared_delays else None,
            'leave_one_out_no_refit': {kind: leave_groups(g,total,len(margins),len(known),low,high,selected) for kind,g in groups.items()}}
    return result


def run(database, poly_root, predictions, prior_report, p):
    validate_plan(p)
    for path, key in ((database,'database_sha256'),(predictions,'prediction_sha256'),(prior_report,'prior_report_sha256')):
        if file_hash(path) != p[key]:
            raise ValueError('frozen_input_changed')
    for path, sha in p['code_sha256'].items():
        if file_hash(Path(__file__).resolve().parents[1] / path) != sha:
            raise ValueError('frozen_code_changed')
    old = json.loads(Path(predictions).read_text()); prior = json.loads(Path(prior_report).read_text())
    if prior['protocol'] != p['prior_hurdle_protocol'] or prior['protocol_sha256'] != p['prior_protocol_sha256']:
        raise ValueError('prior_protocol_changed')
    if old['protocol_sha256'] != prior['protocol']['prior_protocol_sha256']:
        raise ValueError('prediction_lineage_changed')
    if set(old['cohorts']) != set(prior['cohorts']) or set(old['cohorts']) != set(prior['protocol']['cohort_models']):
        raise ValueError('cohort_set_changed')
    cfg = old['plan']['diagnostic']; output = {}
    with connect_readonly(database) as db:
        files = inventory(db); verify_sources(poly_root, files)
        audit = dict(zip(('unique_records','duplicate_repeats','price_conflicts','depth_conflicts'), db.execute(
            'SELECT COUNT(*),COALESCE(SUM(repeats),0),COALESCE(SUM(price_conflict),0),COALESCE(SUM(depth_conflict),0) FROM observations').fetchone()))
        for cohort, data in old['cohorts'].items():
            names = data['models']
            if names != prior['protocol']['cohort_models'][cohort]:
                raise ValueError('candidate_set_changed')
            paths = prior['cohorts'][cohort]['window_ledger']
            if len(paths) != len(data['window_ledger']) or len(paths) != prior['protocol']['cohort_scheduled_windows'][cohort]:
                raise ValueError('scheduled_denominator_changed')
            rows, seen = [], set()
            for original, previous_path in zip(data['window_ledger'], paths):
                if original['market'] in seen:
                    raise ValueError('duplicate_validation_market')
                seen.add(original['market'])
                fold = data['folds'][original['fold']]
                if original['date'] not in fold['validation_dates']:
                    raise ValueError('invalid_fold_date')
                if original.get('predictions') is not None:
                    if set(original['predictions']) != {'bid_change_cents','ask_change_cents'}:
                        raise ValueError('target_set_changed')
                    for target, fits in fold['fits'].items():
                        if set(fits) != set(names) or set(original['predictions'][target]) != set(names):
                            raise ValueError('candidate_set_changed')
                        for name, fit in fits.items():
                            if not math.isclose(predict(fit, original), original['predictions'][target][name], rel_tol=0, abs_tol=1e-12):
                                raise ValueError('frozen_prediction_changed')
                rows.append(attach(db, original, previous_path, cfg))
            output[cohort] = {'window_ledger': rows, 'summaries': {}}
            for side in ('up','down'):
                output[cohort]['summaries'][side] = {view: {str(step): {cost['id']:
                    summarize(rows, names, side, str(step), view, cost) for cost in prior['protocol']['cost_scenarios']}
                    for step in p['entry_steps']} for view in p['views']}
                for cost in prior['protocol']['cost_scenarios']:
                    original_summary = prior['cohorts'][cohort]['sides'][side][cost['id']]['overall']
                    reproduced = output[cohort]['summaries'][side]['price_only']['0'][cost['id']]
                    for name, old_policy in original_summary['policies'].items():
                        now = reproduced['policies'][name]
                        if (now['selected'] != old_policy['selected_windows'] or
                                now['observed_selected'] != old_policy['selected_observed_windows'] or
                                now['unknown_selected'] != old_policy['selected_unknown_windows']):
                            raise ValueError('original_selection_denominator_changed')
                        a, b = now['observed_distribution']['mean'], old_policy['selected_observed_mean_quote_margin_cents']
                        if (a is None) != (b is None) or (a is not None and not math.isclose(a,b,rel_tol=0,abs_tol=1e-12)):
                            raise ValueError('original_quote_margin_changed')
        verify_sources(poly_root, files)
    return {'protocol':p, 'index_audit':audit, 'cohorts':output, 'models_refit':False, 'signals_reselected':False,
            'pnl':None, 'promotion_allowed':False, 'canary_gate_changed':False,
            'limitations':['Already exposed historical samples, not independent OOS.',
             'Recorded sample intervals are not source-time latency, quote freshness or fill evidence.',
             'Leave-out contributions keep coefficients/signals fixed; this is not cross-validation or a significance test.',
             'Costs are hypotheses; positive displayed depth is not executable size or a fill guarantee.',
             'Missing signals and per-scenario unknown paths remain explicit. No evidence does not prove every future variant impossible.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('database','poly-root','predictions','prior-report','protocol','output'):
        parser.add_argument('--'+name, type=Path, required=True)
    a = parser.parse_args(argv)
    result = run(a.database,a.poly_root,a.predictions,a.prior_report,json.loads(a.protocol.read_text()))
    result['protocol_sha256'] = file_hash(a.protocol)
    write_private(a.output,result)
    print('PRIVATE_ROBUSTNESS_COMPLETE; no empirical contents printed.')


if __name__ == '__main__':
    main()
