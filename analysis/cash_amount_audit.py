"""Read-only amount reconciliation. No wallet, network, signing or order methods.

Inputs are supplied evidence, not authenticated bank/onchain truth. Currency is
an explicit settlement asset identity; the quote/fee display unit is not enough.
The caller must provide the ENTIRE account cash-flow slice between checkpoints.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('missing or invalid cash amount') from exc
    if not result.is_finite():
        raise ValueError('non-finite cash amount')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False).encode()).hexdigest()


def audit_cash_amount(opening, flows, closing, required_event_ids, expected_asset):
    """Return a fail-closed diagnostic; never releases external funds.

    `balance_amount` is total SETTLEMENT-TOKEN balance, not an NAV or a claim.
    `available_cash_amount` must equal balance minus externally reserved cash.
    All amounts are decimal strings in the same asset's display units.
    """
    result = dict(amount_verified=False, external_truth_verified=False,
                  orders_enabled=False, reason=None)
    try:
        if not isinstance(opening, dict) or not isinstance(closing, dict):
            raise ValueError('OPENING_AND_CLOSING_CHECKPOINT_REQUIRED')
        if not isinstance(flows, list) or not expected_asset:
            raise ValueError('EXPLICIT_ASSET_AND_CASH_FLOW_SLICE_REQUIRED')
        account = opening.get('account_id')
        if not account or closing.get('account_id') != account:
            raise ValueError('ACCOUNT_IDENTITY_MISMATCH')
        for c in (opening, closing):
            if c.get('cash_asset') != expected_asset:
                raise ValueError('SETTLEMENT_ASSET_MISMATCH')
            if not c.get('balance_checkpoint_id'):
                raise ValueError('CHECKPOINT_REFERENCE_REQUIRED')
        start, end = int(opening['observed_ms']), int(closing['observed_ms'])
        if end < start:
            raise ValueError('REVERSED_CHECKPOINT_TIME')
        initial, final = decimal(opening['balance_amount']), decimal(closing['balance_amount'])
        reserved = decimal(closing['reserved_cash_amount'])
        available = decimal(closing['available_cash_amount'])
        if min(initial, final, reserved, available) < 0 or reserved > final:
            raise ValueError('NEGATIVE_OR_IMPOSSIBLE_BALANCE')
        if abs(final - reserved - available) > Decimal('.000002'):
            raise ValueError('AVAILABLE_BALANCE_RESERVATION_MISMATCH')
        if closing.get('complete_cash_flow_slice') is not True:
            raise ValueError('ACCOUNT_CASH_SLICE_NOT_ATTESTED_COMPLETE')
        deltas, events, ids = [], set(), set()
        for f in flows:
            if not isinstance(f, dict) or not f.get('event_id') or not f.get('economic_id'):
                raise ValueError('CASH_EVENT_IDENTIFIERS_REQUIRED')
            if f['economic_id'] in ids or f['event_id'] in events:
                raise ValueError('DUPLICATE_ECONOMIC_FLOW')
            if f.get('cash_asset') != expected_asset or f.get('account_id') != account:
                raise ValueError('CASH_FLOW_IDENTITY_MISMATCH')
            if f.get('status') != 'CONFIRMED' or not f.get('transaction_ref'):
                raise ValueError('CONFIRMED_CASH_EVIDENCE_REQUIRED')
            if not start < int(f['observed_ms']) <= end:
                raise ValueError('CASH_EVENT_OUTSIDE_CHECKPOINT_INTERVAL')
            d = decimal(f['cash_delta_amount'])
            if f.get('kind') == 'entry_fill' and d >= 0:
                raise ValueError('BUY_MUST_BE_CASH_OUTFLOW')
            if f.get('kind') in ('sell_fill', 'redeem_confirmed') and d < 0:
                raise ValueError('SALE_OR_REDEEM_CANNOT_BE_NEGATIVE_NET_CREDIT')
            events.add(f['event_id']); ids.add(f['economic_id']); deltas.append(d)
        required = {str(x) for x in required_event_ids}
        if not required.issubset(events):
            raise ValueError('MISSING_EPISODE_CASH_AMOUNTS')
        if set(closing.get('covered_event_ids', [])) != events:
            raise ValueError('CASH_SLICE_COVERAGE_MISMATCH')
        if not closing.get('position_checkpoint_id') or decimal(closing['reconciled_position_shares']) != 0:
            raise ValueError('ZERO_POSITION_CHECKPOINT_REQUIRED')
        expected = initial + sum(deltas, Decimal(0))
        diff = final - expected
        result.update(expected_balance=str(expected), observed_balance=str(final),
                      difference=str(diff), available_cash=str(available),
                      reserved_cash=str(reserved), flow_count=len(flows))
        if abs(diff) > Decimal('.000002'):
            raise ValueError('CASH_AMOUNT_MISMATCH')
        result.update(amount_verified=True, reason='AMOUNT_AND_IDENTITY_CONSISTENT',
                      evidence_hash=fingerprint(dict(opening=opening, flows=flows, closing=closing)),
                      note='Checks supplied evidence only; actual chain and account provenance must be verified separately.')
    except (KeyError, TypeError, ValueError) as exc:
        result['reason'] = str(exc)
    return result
