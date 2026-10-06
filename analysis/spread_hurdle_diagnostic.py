"""Frozen quote-path spread hurdles, never fills, PnL or portfolio simulation.

Consumes existing walk-forward predictions without fitting or selecting models.
All directions/cost assumptions are fixed before evaluation. Down prices always
come from the recorded Down book, never a complement of the Up book.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics

try:
    from archive_coverage_ledger import connect_readonly, inventory, verify_sources
    from archive_baseline_audit import write_private
    from historical_walk_forward import file_hash
    from quote_factor_diagnostic import predict
except ImportError:
    from analysis.archive_coverage_ledger import connect_readonly, inventory, verify_sources
    from analysis.archive_baseline_audit import write_private
    from analysis.historical_walk_forward import file_hash
    from analysis.quote_factor_diagnostic import predict


def validate_plan(p):
    if p['signal_rule'] != 'strict_positive_predicted_spread_headroom' or p['down_score'] != 'negative_frozen_up_ask_change_directional_proxy':
        raise ValueError('unsupported_signal_rule')
    if p['selection'] != 'none_all_candidates_and_scenarios' or p['gate_cost_scenario'] != 'zero':
        raise ValueError('selection_or_cost_dependent_gate_forbidden')
    if type(p['budget_cents']) not in (int, float) or not math.isfinite(p['budget_cents']) or p['budget_cents'] <= 0:
        raise ValueError('invalid_budget')
    names = set()
    for s in p['cost_scenarios']:
        if s['id'] in names:
            raise ValueError('duplicate_cost_scenario')
        names.add(s['id'])
        for key in ('fee_cents_per_share_per_leg', 'slippage_cents_per_share_per_leg'):
            if type(s[key]) not in (int, float) or not math.isfinite(s[key]) or s[key] < 0:
                raise ValueError('invalid_assumed_cost')
        if s.get('historical_actual_cost') is not False:
            raise ValueError('unverified_cost_cannot_be_historical_actual')
    zero = next((s for s in p['cost_scenarios'] if s['id'] == 'zero'), None)
    if zero is None or zero['fee_cents_per_share_per_leg'] or zero['slippage_cents_per_share_per_leg']:
        raise ValueError('missing_zero_cost_optimistic_scenario')


def quote_at(db, market, ts):
    row = db.execute('SELECT ts_us,start,prices,sizes,price_reason FROM observations WHERE slug=? AND ts_us=?',
                     (market, round(ts * 1000000))).fetchone()
    if row is None:
        raise ValueError('frozen_quote_timestamp_missing')
    return {'ts': row[0] / 1000000, 'start': row[1], 'prices': json.loads(row[2]),
            'sizes': json.loads(row[3]), 'price_reason': row[4]}


def attach_path(db, original, cfg):
    """Join exact causal quotes; any disagreement blocks, never shrinks cohort."""
    row = {k: original[k] for k in ('market', 'date', 'start', 'fold', 'status')}
    row.update(eligible=original.get('predictions') is not None, predictions=original.get('predictions'), sides=None)
    if not row['eligible']:
        return row
    if not original.get('features') or not original.get('feature_times'):
        raise ValueError('prediction_without_causal_features')
    current = quote_at(db, row['market'], original['feature_times'][-1])
    lag = quote_at(db, row['market'], original['feature_times'][0])
    decision = original['decision_time']
    if current['start'] != row['start'] or lag['start'] != row['start']:
        raise ValueError('cross_window_feature')
    if not 0 < decision - current['ts'] <= cfg['max_quote_age_seconds']:
        raise ValueError('noncausal_or_stale_decision_quote')
    lag_age = decision - cfg['lookback_seconds'] - lag['ts']
    if not 0 <= lag_age <= cfg['max_quote_age_seconds'] or current['price_reason'] or lag['price_reason']:
        raise ValueError('invalid_feature_quote')
    ask, bid = current['prices'][:2]
    features = {'momentum': (ask + bid - lag['prices'][0] - lag['prices'][1]) / 2, 'spread': ask - bid}
    if 'imbalance' in original['features']:
        sizes = current['sizes']
        if any(x is None or x <= 0 for x in sizes + lag['sizes']):
            raise ValueError('depth_feature_without_depth')
        features['imbalance'] = (sizes[1] - sizes[0]) / (sizes[1] + sizes[0])
    if features != original['features']:
        raise ValueError('frozen_feature_mismatch')
    low = round((decision + cfg['horizon_seconds']) * 1000000)
    raw = db.execute('SELECT ts_us,start,prices,price_reason FROM observations WHERE slug=? AND ts_us>=? ORDER BY ts_us LIMIT 1',
                     (row['market'], low)).fetchone()
    future = None
    future_status = 'unknown_future_missing'
    if raw is not None and raw[0] / 1000000 <= decision + cfg['horizon_seconds'] + cfg['future_tolerance_seconds']:
        if raw[1] != row['start'] or raw[0] >= (row['start'] + 300) * 1000000:
            raise ValueError('cross_window_future')
        future_status = 'unknown_future_price' if raw[3] else 'labeled_quote_change'
        if raw[3] is None:
            future = json.loads(raw[2])
    if future_status != original['status']:
        raise ValueError('frozen_label_cohort_mismatch')
    if future is not None:
        targets = {'bid_change_cents': future[1] - bid, 'ask_change_cents': future[0] - ask}
        if targets != original['targets'] or raw[0] / 1000000 != original['target_time']:
            raise ValueError('frozen_target_mismatch')
    elif original['targets'] is not None:
        raise ValueError('unexpected_frozen_label')
    row['sides'] = {}
    for side, i in (('up', 0), ('down', 2)):
        a, b = current['prices'][i:i + 2]
        row['sides'][side] = {'ask_cents': a, 'bid_cents': b, 'spread_cents': a - b,
                            'reported_ask_size': current['sizes'][i],
                            'future_bid_cents': future[i + 1] if future is not None else None,
                            'observed_spread_headroom_cents': future[i + 1] - a if future is not None else None}
    row.update(sample_time=current['ts'], future_sample_time=raw[0] / 1000000 if future is not None else None,
               future_status=future_status, historical_rules_verified=False, source_receive_time_verified=False)
    return row


def signal(row, name, side):
    if not row['eligible'] or name == 'no_signal':
        return False
    if name == 'always_same_side':
        return True
    target = 'bid_change_cents' if side == 'up' else 'ask_change_cents'
    score = row['predictions'][target][name] * (1 if side == 'up' else -1)
    return score > row['sides'][side]['spread_cents']


def directional_score(row, name, side):
    if name in ('always_same_side', 'no_signal'):
        return None
    target = 'bid_change_cents' if side == 'up' else 'ask_change_cents'
    return row['predictions'][target][name] * (1 if side == 'up' else -1)


def budget_example(book, budget, entry_cost):
    entry = book['ask_cents'] + entry_cost
    quoted_cap = math.floor(budget / entry)
    size = book['reported_ask_size']
    usable_size = size is not None and math.isfinite(size) and size >= 0
    return {'one_share_quote_cost_cents': entry, 'one_share_quote_fits_budget': entry <= budget,
            'one_share_reported_depth_sufficient': size >= 1 if usable_size else None,
            'budget_whole_share_cap': quoted_cap,
            'displayed_whole_share_cap': min(quoted_cap, math.floor(size)) if usable_size else None,
            'size_as_shares_assumption': True, 'minimum_order_verified': False, 'order_feasibility': None}


def aggregate(rows, names, side, scenario, budget):
    eligible = [x for x in rows if x['eligible']]
    known = [x for x in eligible if x['sides'][side]['future_bid_cents'] is not None]
    unknown = [x for x in eligible if x['sides'][side]['future_bid_cents'] is None]
    entry_cost = scenario['fee_cents_per_share_per_leg'] + scenario['slippage_cents_per_share_per_leg']
    cost = 2 * entry_cost
    result = {'scheduled_windows': len(rows), 'eligible_windows': len(eligible), 'shared_labeled_windows': len(known),
              'unknown_future_quotes': len(unknown), 'unobservable_signal_windows': len(rows) - len(eligible),
              'frozen_status_counts': dict(Counter(x['status'] for x in rows)), 'policies': {}}
    for name in list(names) + ['always_same_side', 'no_signal']:
        selected = [x for x in eligible if signal(x, name, side)]
        selected_known = [x for x in known if signal(x, name, side)]
        selected_unknown = len(selected) - len(selected_known)
        margins = [x['sides'][side]['observed_spread_headroom_cents'] - cost for x in selected_known]
        total = sum(margins)
        lower = total - sum(x['sides'][side]['ask_cents'] + cost for x in unknown if signal(x, name, side))
        upper = total + sum(100 - x['sides'][side]['ask_cents'] - cost for x in unknown if signal(x, name, side))
        budgets = [budget_example(x['sides'][side], budget, entry_cost) for x in selected]
        depth_caps = [x['displayed_whole_share_cap'] for x in budgets if x['displayed_whole_share_cap'] is not None]
        scores = [directional_score(x, name, side) for x in eligible] if name in names else []
        selected_scores = [directional_score(x, name, side) for x in selected] if name in names else []
        missing_exposure = len(rows) - len(eligible) if name != 'no_signal' else 0
        paired = {}
        for baseline in ('always_same_side', 'no_signal'):
            value = sum((int(signal(x, name, side)) - int(signal(x, baseline, side))) *
                        (x['sides'][side]['observed_spread_headroom_cents'] - cost) for x in known)
            low = high = value
            for x in unknown:
                sign = int(signal(x, name, side)) - int(signal(x, baseline, side))
                ask = x['sides'][side]['ask_cents']
                ends = [sign * (y - ask - cost) for y in (0, 100)]
                low += min(ends)
                high += max(ends)
            paired[baseline] = {'same_label_mean_cents': value / len(known) if known else None,
                                'eligible_lower_cents': low / len(eligible) if eligible else None,
                                'eligible_upper_cents': high / len(eligible) if eligible else None}
        result['policies'][name] = {
            'selected_windows': len(selected), 'selected_observed_windows': len(selected_known),
            'selected_unknown_windows': selected_unknown,
            'selected_observed_mean_quote_margin_cents': statistics.mean(margins) if margins else None,
            'eligible_mean_predicted_directional_score_cents': statistics.mean(scores) if scores else None,
            'eligible_mean_spread_cents': statistics.mean(x['sides'][side]['spread_cents'] for x in eligible) if eligible else None,
            'selected_mean_predicted_directional_score_cents': statistics.mean(selected_scores) if selected_scores else None,
            'selected_mean_spread_cents': statistics.mean(x['sides'][side]['spread_cents'] for x in selected) if selected else None,
            'selected_observed_positive_fraction': sum(x > 0 for x in margins) / len(margins) if margins else None,
            'selected_eligible_lower_cents': lower / len(selected) if selected else None,
            'selected_eligible_upper_cents': upper / len(selected) if selected else None,
            'shared_label_activity_weighted_mean_cents': total / len(known) if known else None,
            'eligible_activity_weighted_lower_cents': lower / len(eligible) if eligible else None,
            'eligible_activity_weighted_upper_cents': upper / len(eligible) if eligible else None,
            'full_calendar_missing_exposure_lower_cents': (lower - missing_exposure * (100 + cost)) / len(rows) if rows else None,
            'full_calendar_missing_exposure_upper_cents': (upper + missing_exposure * max(0, 100 - cost)) / len(rows) if rows else None,
            'paired_vs': paired,
            'budget_illustration': {'selected_windows': len(budgets),
                'one_share_quote_fits_budget_count': sum(x['one_share_quote_fits_budget'] for x in budgets),
                'one_share_reported_depth_sufficient_count': sum(x['one_share_reported_depth_sufficient'] is True for x in budgets),
                'depth_unknown_count': sum(x['one_share_reported_depth_sufficient'] is None for x in budgets),
                'displayed_whole_share_cap_min': min(depth_caps) if depth_caps else None,
                'displayed_whole_share_cap_median': statistics.median(depth_caps) if depth_caps else None,
                'displayed_whole_share_cap_max': max(depth_caps) if depth_caps else None,
                'minimum_order_verified': False, 'order_feasibility': None}}
        result['policies'][name]['diagnostic_status'] = (
            'no_signal' if not selected else 'no_observed_selected_labels' if not margins else
            ('failed_even_zero_cost_quote_hurdle' if cost == 0 else 'failed_under_assumed_cost')
            if statistics.mean(margins) <= 0 else
            'positive_observed_only_missingness_bound_not_positive' if lower <= 0 else
            'positive_eligible_quote_margin_bound_not_execution_evidence')
    return result


def summarize(rows, names, p):
    report = {'sides': {}, 'window_ledger': rows}
    for side in ('up', 'down'):
        days = defaultdict(list)
        for row in rows:
            days[row['date']].append(row)
        report['sides'][side] = {}
        for scenario in p['cost_scenarios']:
            report['sides'][side][scenario['id']] = {
                'assumptions': scenario, 'overall': aggregate(rows, names, side, scenario, p['budget_cents']),
                'by_date': {date: aggregate(group, names, side, scenario, p['budget_cents']) for date, group in sorted(days.items())}}
    return report


def run(database, source_root, prediction_file, p):
    validate_plan(p)
    if file_hash(database) != p['database_sha256'] or file_hash(prediction_file) != p['prediction_file_sha256']:
        raise ValueError('frozen_input_changed')
    repo = Path(__file__).resolve().parents[1]
    for name, sha in p['code_sha256'].items():
        if file_hash(repo / name) != sha:
            raise ValueError('frozen_code_changed')
    prior = json.loads(Path(prediction_file).read_text())
    if prior['protocol_sha256'] != p['prior_protocol_sha256'] or prior['plan']['database_sha256'] != p['database_sha256']:
        raise ValueError('prior_lineage_mismatch')
    cfg = prior['plan']['diagnostic']
    if set(prior['cohorts']) != set(p['cohort_models']) or set(prior['cohorts']) != set(p['cohort_scheduled_windows']):
        raise ValueError('frozen_cohort_set_changed')
    output = {}
    with connect_readonly(database) as db:
        files = inventory(db)
        verify_sources(source_root, files)
        for cohort, data in prior['cohorts'].items():
            if data['models'] != p['cohort_models'][cohort]:
                raise ValueError('candidate_family_changed')
            rows, seen = [], set()
            for old in data['window_ledger']:
                if old['market'] in seen:
                    raise ValueError('duplicate_validation_market')
                seen.add(old['market'])
                fold = data['folds'][old['fold']]
                if old['date'] not in fold['validation_dates']:
                    raise ValueError('outside_frozen_validation_dates')
                if old.get('predictions') is not None:
                    for target, fits in fold['fits'].items():
                        if set(fits) != set(data['models']) or set(old['predictions'][target]) != set(fits):
                            raise ValueError('prediction_model_set_changed')
                        for name, model in fits.items():
                            if not math.isclose(predict(model, old), old['predictions'][target][name], rel_tol=0, abs_tol=1e-12):
                                raise ValueError('frozen_prediction_changed')
                rows.append(attach_path(db, old, cfg))
            if len(rows) != p['cohort_scheduled_windows'][cohort]:
                raise ValueError('scheduled_denominator_changed')
            output[cohort] = summarize(rows, data['models'], p)
        verify_sources(source_root, files)
    return {'scope': 'historically exposed quote-based spread hurdle; not realized or simulated trading PnL',
            'protocol': p, 'cohorts': output, 'models_refit': False, 'source_bytes_unchanged': True,
            'capital_path_simulated': False, 'fills_simulated': False, 'pnl': None,
            'limitations': [
                'Up score is frozen Up bid-change prediction; Down score is reversed Up ask-change directional proxy, not a calibrated Down price forecast.',
                'Up and Down are separate hypotheses, not a combined portfolio or competing order selection.',
                'Actual archived side-specific ask and future bid are used; no complement-generated Down quotes.',
                'Signals use zero-cost spread hurdle and are identical across all cost assumptions; no thresholds searched or model selected.',
                'Activity-weighted means use the same labeled windows across all policies. Selected-only means have different selections and are not paired skill comparisons.',
                'Unknown future bids range over [0,100]; unobservable-signal calendar bounds assume one missing share exposure in the worst case, not actual PnL.',
                'Reported size is treated as shares only for a static budget illustration; minimum order, fee rules, tick size, source/receive timing and fills remain unverified.',
                'No capital reuse, settlement, concurrent positions, turnover, actual fees or account balance is inferred.']}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--poly-root', type=Path, required=True)
    p.add_argument('--predictions', type=Path, required=True)
    p.add_argument('--protocol', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(argv)
    result = run(a.database, a.poly_root, a.predictions, json.loads(a.protocol.read_text()))
    result['protocol_sha256'] = file_hash(a.protocol)
    write_private(a.output, result)
    print('PRIVATE_SPREAD_HURDLE_COMPLETE; no empirical contents printed.')


if __name__ == '__main__':
    main()
