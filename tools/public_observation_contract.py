"""Normalize supplied public-wallet observations without inventing trading skill.
No network, signal, order, account access or profit estimator. Caller must supply
identities, timestamps and canonical settlement event fields explicitly.
"""
from decimal import Decimal, InvalidOperation


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('nonnegative explicit integer required')
    return value


def assess(event, *, decision_ms, cohort_locked_ms, cohort_cutoff_ms, cohort_wallets, condition_id, outcome_tokens):
    out = {'usable_asof_observation': False, 'source_authenticated': False,
           'wallet_quote_lifecycle_observed': False, 'is_trade_recommendation': False,
           'copy_return_verified': False, 'reason': None}
    try:
        now, locked, cutoff = map(integer, (decision_ms, cohort_locked_ms, cohort_cutoff_ms))
        if not cutoff <= locked <= now:
            raise ValueError('COHORT_NOT_FROZEN_BEFORE_DECISION')
        if event.get('wallet') not in cohort_wallets:
            raise ValueError('OUTSIDE_FROZEN_COHORT')
        source, received = integer(event.get('event_ms')), integer(event.get('received_ms'))
        if source > received or received > now:
            raise ValueError('NOT_AVAILABLE_ASOF_OR_CLOCK_CONFLICT')
        if event.get('condition_id') != condition_id or event.get('token_id') not in outcome_tokens:
            raise ValueError('MARKET_IDENTITY_MISMATCH')
        if not isinstance(event.get('transaction_hash'), str) or not event['transaction_hash']:
            raise ValueError('MISSING_SETTLEMENT_REFERENCE')
        index = integer(event.get('log_index'))
        chain = integer(event.get('chain_id'))
        if event.get('role') not in {'maker', 'taker'} or event.get('role_source') != 'settlement':
            raise ValueError('ROLE_NOT_SETTLEMENT_VERIFIED')
        if event.get('kind') != 'fill':
            raise ValueError('MINT_MERGE_TRANSFER_IS_NOT_DIRECTIONAL_FILL')
        if event.get('side') not in {'BUY', 'SELL'}:
            raise ValueError('SIDE_UNKNOWN')
        q, p = Decimal(str(event.get('shares'))), Decimal(str(event.get('price')))
        if not q.is_finite() or not p.is_finite() or q <= 0 or not 0 < p < 1:
            raise ValueError('INVALID_SHARE_OR_PRICE_UNITS')
        out.update(usable_asof_observation=True, reason='SUPPLIED_EVENT_CONSISTENT_NOT_AUTHENTICATED',
                   economic_event_id=f"{chain}:{event['transaction_hash']}:{index}:{event['wallet']}",
                   observed_notional=str(q*p), observation_lag_ms=received-source)
    except (ValueError, TypeError, InvalidOperation):
        # Fixed error messages contain no account values.
        import sys
        text = str(sys.exc_info()[1])
        out['reason'] = text if text.isupper() else 'INVALID_REQUIRED_FIELD'
    return out
