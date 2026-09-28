"""Generic finite-bankroll arithmetic over SUPPLIED net-return distributions.

No prices, wallet list, empirical findings, fitting, account client or orders.
Probabilities and legal stake amounts are assumptions, not authenticated facts.
A positive expectation does not certify a profitable or executable strategy.

References: Busseti/Ryu/Boyd, arXiv:1603.06183; Long, arXiv:2604.11577.
The discrete-stake calculations below are elementary, not a reproduction of
those papers' risk-constrained optimizers or a drawdown-probability guarantee.
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from pathlib import Path

MAX_STATES = 10000
MAX_STAKES = 10000


def number(x):
    if isinstance(x, bool):
        raise ValueError('BOOLEAN_IS_NOT_A_NUMBER')
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('FINITE_NUMBER_REQUIRED') from exc
    if not math.isfinite(v):
        raise ValueError('FINITE_NUMBER_REQUIRED')
    return v


def states_checked(states):
    if not isinstance(states, list) or not 1 <= len(states) <= MAX_STATES:
        raise ValueError('BOUNDED_NONEMPTY_DISTRIBUTION_REQUIRED')
    result = []
    for s in states:
        if not isinstance(s, dict):
            raise ValueError('STATE_OBJECT_REQUIRED')
        # Unknown returns/time must not silently become zero cash or zero time.
        p, r, t = (number(s[k]) for k in
                   ('probability', 'net_return', 'cash_cycle_seconds'))
        if not 0 <= p <= 1 or r < -1 or t <= 0:
            raise ValueError('INVALID_PROBABILITY_RETURN_OR_DURATION')
        result.append((p, r, t))
    if not math.isclose(math.fsum(s[0] for s in result), 1.0,
                        rel_tol=0, abs_tol=1e-12):
        raise ValueError('PROBABILITIES_MUST_SUM_TO_ONE')
    return result


def evaluate_stake(wealth, stake, states):
    """One-cycle arithmetic; log/time is a conditional renewal ratio only.

    wealth is equity on a CONSISTENT net valuation basis. stake is all-in cash
    at risk. net_return includes every cost once. All durations include waiting
    until the next usable cash checkpoint, including zero-return rejections.
    No independence, stationarity or correct probabilities are inferred.
    """
    w, s = number(wealth), number(stake)
    if w <= 0 or not 0 <= s <= w:
        raise ValueError('NO_BORROWING_AND_POSITIVE_EQUITY_REQUIRED')
    xs = states_checked(states)
    f = s / w
    expected_pnl = s * math.fsum(p * r for p, r, _ in xs)
    seconds = math.fsum(p * t for p, _, t in xs)
    ruined = any(p > 0 and 1 + f * r <= 0 for p, r, _ in xs)
    log_growth = None if ruined else math.fsum(
        p * math.log1p(f * r) for p, r, _ in xs if p > 0)
    return {
        'stake': s, 'fraction_of_equity': f,
        'expected_net_cash_pnl': expected_pnl,
        'expected_log_change': log_growth,
        'log_status': 'NEGATIVE_INFINITY_POSSIBLE_RUIN' if ruined else 'FINITE',
        'expected_cash_cycle_seconds': seconds,
        'log_change_per_expected_second': None if ruined else log_growth / seconds,
        'one_cycle_geometric_equivalent_return': -1.0 if ruined else math.expm1(log_growth),
        'positive_arithmetic_expectation': expected_pnl > 0,
        'positive_log_expectation': False if ruined else log_growth > 0,
        'empirical_edge_verified': False,
        'renewal_assumptions_verified': False,
        'orders_enabled': False,
    }


def binary_states(p, win_return, loss_fraction, seconds=1):
    p, b, l, t = map(number, (p, win_return, loss_fraction, seconds))
    if not 0 <= p <= 1 or b <= 0 or not 0 < l <= 1 or t <= 0:
        raise ValueError('INVALID_BINARY_SCENARIO')
    return [dict(probability=p, net_return=b, cash_cycle_seconds=t),
            dict(probability=1-p, net_return=-l, cash_cycle_seconds=t)]


def binary_thresholds(p, win_return, loss_fraction, fraction):
    """Two NET outcomes only; do not substitute final-event odds for exits."""
    p, b, l, f = map(number, (p, win_return, loss_fraction, fraction))
    binary_states(p, b, l)
    if not 0 < f <= 1:
        raise ValueError('POSITIVE_UNLEVERED_FRACTION_REQUIRED')
    cash_threshold = l / (b + l)
    down = 1 - f * l
    if down <= 0:
        log_threshold = 1.0  # Any positive loss probability causes log ruin.
    else:
        a, c = math.log1p(f*b), math.log1p(-f*l)
        log_threshold = -c / (a-c)
    continuous_fraction = max(0.0, min(1.0, (p*b-(1-p)*l)/(b*l)))
    return {
        'cash_break_even_win_probability': cash_threshold,
        'log_break_even_win_probability_at_fraction': log_threshold,
        'continuous_kelly_fraction_supplied_binary_model': continuous_fraction,
        'log_threshold_boundary_requires_p_one': down <= 0,
        'model_scope': 'two_net_outcomes_only_no_estimation_or_execution',
    }


def minimum_bankroll_boundary(minimum_stake, p, win_return, loss_fraction):
    """Boundary W for positive log expectation at the minimum stake.

    The nontrivial root is found by bisection of a concave two-outcome log
    function. This is NOT a recommendation to add capital: bad/uncertain edge
    is not fixed by a larger bankroll. Floating-point boundary is approximate.
    """
    m, p, b, l = map(number, (minimum_stake, p, win_return, loss_fraction))
    if m <= 0:
        raise ValueError('POSITIVE_MINIMUM_REQUIRED')
    xs = binary_states(p, b, l)
    edge = p*b-(1-p)*l
    if edge <= 0:
        return dict(status='NO_POSITIVE_GROWTH_IN_SUPPLIED_MODEL', bankroll_boundary=None)
    def g(f):
        r = evaluate_stake(1.0, f, xs)['expected_log_change']
        return -math.inf if r is None else r
    if g(1) > 0:
        return dict(status='MINIMUM_FEASIBLE_BANKROLL_SUFFICES',
                    bankroll_boundary=m, strict_greater_than=False)
    lo = min(1.0, edge/(b*l))
    hi = 1.0
    if g(lo) <= 0:
        return dict(status='NUMERICAL_EDGE_TOO_SMALL', bankroll_boundary=None)
    for _ in range(100):
        mid = (lo+hi)/2
        if mid == lo or mid == hi:
            break
        if g(mid) > 0:
            lo = mid
        else:
            hi = mid
    return dict(status='POSITIVE_GROWTH_REQUIRES_BANKROLL_ABOVE_BOUNDARY',
                bankroll_boundary=m/hi, strict_greater_than=True,
                zero_log_stake_fraction=hi)


def compare_supplied_stakes(*, equity, available_cash, minimum_stake,
                            all_in_cap, legal_stakes, states):
    """Always compare cash to user-supplied legal lots; NEVER round up Kelly.

    'legal_stakes' is only a caller assertion. Exchange quantities, fees,
    price grids, asset units and available funds need separate verification.
    """
    w, cash, minimum, cap = map(number, (equity, available_cash, minimum_stake, all_in_cap))
    if w <= 0 or not 0 <= cash <= w or minimum <= 0 or cap <= 0:
        raise ValueError('INVALID_EQUITY_CASH_MINIMUM_OR_CAP')
    xs = states_checked(states)
    if not isinstance(legal_stakes, list) or len(legal_stakes) > MAX_STAKES:
        raise ValueError('BOUNDED_SUPPLIED_LOTS_REQUIRED')
    amounts = [number(x) for x in legal_stakes]
    if len(set(amounts)) != len(amounts):
        raise ValueError('DUPLICATE_LOT')
    if any(s < 0 or (s > 0 and s < minimum) for s in amounts):
        raise ValueError('LOT_BELOW_SUPPLIED_MINIMUM')
    reasons = []
    if minimum > cash:
        reasons.append('MINIMUM_EXCEEDS_AVAILABLE_CASH')
    if minimum > cap:
        reasons.append('MINIMUM_EXCEEDS_RISK_CAP')
    rows, rejected = [], []
    for s in sorted(set([0.0] + amounts)):
        if s > min(cash, cap):
            rejected.append({'stake': s, 'reason': 'CASH_OR_CAP_CONSTRAINT'})
            continue
        rows.append(evaluate_stake(w, s, states))
    # Mathematical max over supplied alternatives, not a trade signal.
    def score(r):
        return -math.inf if r['expected_log_change'] is None else r['expected_log_change']
    best = max(rows, key=lambda r: (score(r), -r['stake']))
    if best['stake'] == 0 and not reasons:
        reasons.append('CASH_MAXIMIZES_SUPPLIED_DISCRETE_SCENARIOS')
    return {'schema': 1, 'scope': 'supplied_scenario_arithmetic_only',
            'rows': rows, 'rejected_lots': rejected, 'reasons': reasons,
            'argmax_stake_in_supplied_scenarios': best['stake'],
            'exchange_legality_verified': False, 'probabilities_verified': False,
            'cash_receipt_verified': False, 'human_canary_approved': False,
            'orders_enabled': False}


def main():
    ap = argparse.ArgumentParser(description='Local scenario diagnostics; sealed output only.')
    ap.add_argument('--input', required=True)
    ap.add_argument('--recipient', required=True)
    ap.add_argument('--recipient-id', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    try:
        # Key validation precedes reading user inputs; no plaintext fallback.
        from research_vault import (load_json_bytes, read_bounded, validate_recipient,
                                    require_private_location, seal, canonical, write_new)
        recipient = load_json_bytes(read_bounded(args.recipient, 4096), 4096)
        validate_recipient(recipient, args.recipient_id)
        inp = require_private_location(args.input)
        payload = load_json_bytes(read_bounded(inp, 2*1024*1024), 2*1024*1024)
        if payload.get('net_costs_included') is not True:
            raise ValueError('EXPLICIT_NET_COST_CONVENTION_REQUIRED')
        keys = ('equity','available_cash','minimum_stake','all_in_cap','legal_stakes','states')
        result = compare_supplied_stakes(**{k: payload[k] for k in keys})
        if not args.output.endswith('.vault'):
            raise ValueError('SEALED_EXTENSION_REQUIRED')
        write_new(args.output, seal(canonical(result), recipient, args.recipient_id), 0o644)
        print('SEALED_SCENARIO_DIAGNOSTIC_WRITTEN; NOT_A_TRADING_APPROVAL')
    except Exception:
        print('OPERATION_FAILED; NO_PLAINTEXT_FALLBACK', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
