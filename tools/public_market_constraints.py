"""Pure parsing of bounded public BTC five-minute market constraint evidence.

No transport, orders, wallets or strategy logic. API shapes are documented at:
https://docs.polymarket.com/api-reference/feeds/list-trades
https://docs.polymarket.com/market-data/market-details#market-outcomes
https://docs.polymarket.com/api-reference/markets/get-clob-market-info
https://docs.polymarket.com/api-reference/market-data/get-order-book
https://docs.polymarket.com/trading/place-orders
"""
from __future__ import annotations

import base64
from decimal import Decimal, InvalidOperation
import json
import re

SLUG = re.compile(r'btc-updown-5m-([1-9][0-9]{9})\Z')
CONDITION = re.compile(r'0x[0-9a-fA-F]{64}\Z')
TOKEN = re.compile(r'[1-9][0-9]{0,77}\Z')
BOOK_DOC = 'https://docs.polymarket.com/trading/place-orders'
GAMMA_CONSTRAINT_DOC = 'https://docs.polymarket.com/market-data/market-details#trading-constraints'


def require(value, code):
    if not value:
        raise ValueError(code)


def slug_start_ms(value):
    match = SLUG.fullmatch(value) if isinstance(value, str) else None
    require(match is not None and int(match[1]) % 300 == 0, 'INVALID_BTC5M_SLUG')
    return int(match[1]) * 1000


def condition_id(value):
    require(isinstance(value, str) and CONDITION.fullmatch(value), 'INVALID_CONDITION_ID')
    return value.lower()


def token_id(value):
    require(isinstance(value, str) and TOKEN.fullmatch(value) and int(value) < 2 ** 256, 'INVALID_TOKEN_ID')
    return value


def strict_json(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def reject(value):
        raise ValueError('NONFINITE_JSON_NUMBER')
    return json.loads(body, object_pairs_hook=unique, parse_float=Decimal, parse_constant=reject)


def record_document(record):
    require(record.get('http_status') == 200 and record.get('exception_code') is None
            and record.get('body_exceeded_cap') is False and type(record.get('received_ms')) is int,
            'RESPONSE_UNAVAILABLE')
    body = base64.b64decode(record['raw_body_base64'], validate=True)
    require(len(body) <= 1024 * 1024, 'BODY_LIMIT')
    document = strict_json(body)
    require(isinstance(document, dict), 'OBJECT_REQUIRED')
    return document


def clock_sequence_valid(records, finished_at_ms):
    """Verify causal wall-clock order across the complete sequential request log."""
    if type(finished_at_ms) is not int or finished_at_ms < 0:
        return False
    previous_finish = 0
    for record in records:
        started, received, finished = (record.get(key) for key in
                                       ('request_started_ms', 'received_ms', 'finished_ms'))
        if any(type(value) is not int or value < 0 for value in (started, finished)):
            return False
        if received is not None and (type(received) is not int or not started <= received <= finished):
            return False
        elapsed = record.get('monotonic_elapsed_ns')
        if type(elapsed) is not int or elapsed < 0:
            return False
        if not previous_finish <= started <= finished <= finished_at_ms:
            return False
        previous_finish = finished
    return True


def select_current_market(records, selected_at_ms):
    """Select identity only, from the existing prefix, without price/PnL ranking."""
    require(type(selected_at_ms) is int and selected_at_ms > 0, 'INVALID_SELECTION_TIME')
    result = {'status': 'no_current_btc5m_in_observed_prefix', 'selected_at_ms': selected_at_ms,
              'selection_source_request_count': len(records),
              'selection_rule': 'unique_current_btc5m_identity_in_fixed_global_prefix; no_price_or_profit_selection',
              'rejected_current_identity_rows': 0, 'unusable_trade_responses': 0,
              'prefix_is_complete_history': False}
    if not clock_sequence_valid(records, selected_at_ms):
        result['status'] = 'receipt_clock_order_unverified'
        return result
    identities, rejected, malformed = {}, 0, 0
    for request_index, record in enumerate(records):
        if record.get('endpoint') != '/v2/trades':
            continue
        try:
            document = record_document(record)
            require(isinstance(document.get('data'), list)
                    and all(isinstance(row, dict) for row in document['data']), 'TRADE_DATA_REQUIRED')
            pagination = document.get('pagination')
            require(isinstance(pagination, dict) and type(pagination.get('has_more')) is bool
                    and 'next_cursor' in pagination, 'TRADE_PAGINATION_REQUIRED')
            cursor = pagination['next_cursor']
            require((isinstance(cursor, str) and 0 < len(cursor) <= 8192) if pagination['has_more']
                    else cursor is None, 'TRADE_PAGINATION_INVALID')
        except (ValueError, TypeError, KeyError):
            malformed += 1
            continue
        for row_index, row in enumerate(document['data']):
            if not isinstance(row, dict):
                continue
            try:
                start = slug_start_ms(row.get('slug'))
            except ValueError:
                continue
            if not start <= selected_at_ms < start + 300_000:
                continue
            try:
                condition, token = condition_id(row.get('condition_id')), token_id(row.get('token_id'))
                outcome = row.get('outcome_index')
                require(type(outcome) is int and outcome in (0, 1), 'INVALID_OUTCOME_INDEX')
            except ValueError:
                rejected += 1
                continue
            identity = (row['slug'], condition)
            identities.setdefault(identity, []).append({'token_id': token, 'outcome_index': outcome,
                                                        'source_request_index': request_index, 'source_row_index': row_index})
    result.update(rejected_current_identity_rows=rejected, unusable_trade_responses=malformed)
    if rejected or len(identities) > 1:
        result['status'] = 'ambiguous_or_invalid_current_identity'
    elif len(identities) == 1:
        (slug, condition), trades = next(iter(identities.items()))
        tokens = {}
        outcomes = {}
        for trade in trades:
            tokens.setdefault(trade['token_id'], set()).add(trade['outcome_index'])
            outcomes.setdefault(trade['outcome_index'], set()).add(trade['token_id'])
        if any(len(values) != 1 for values in list(tokens.values()) + list(outcomes.values())):
            result['status'] = 'ambiguous_or_invalid_current_identity'
        else:
            start = slug_start_ms(slug)
            result.update(status='selected', slug=slug, condition_id=condition,
                          window_start_ms=start, window_end_ms=start + 300_000, observed_trades=trades)
    return result


def gamma_binding(document, selection):
    require(document.get('slug') == selection['slug']
            and condition_id(document.get('conditionId')) == selection['condition_id'], 'GAMMA_IDENTITY_CONFLICT')
    version = document.get('version')
    require(version in ('v1', 'v2'), 'GAMMA_VERSION_UNKNOWN')
    if version == 'v2':
        tokens = document.get('positionIds')
    else:
        raw = document.get('clobTokenIds')
        require(isinstance(raw, str), 'V1_TOKEN_ARRAY_ENCODING_UNKNOWN')
        tokens = strict_json(raw)
    require(isinstance(tokens, list) and len(tokens) == 2, 'BINARY_TOKEN_ARRAY_REQUIRED')
    tokens = [token_id(value) for value in tokens]
    require(len(set(tokens)) == 2, 'DUPLICATE_TOKEN_ID')
    outcomes = document.get('outcomes')
    require(isinstance(outcomes, str), 'OUTCOME_ARRAY_ENCODING_UNKNOWN')
    outcomes = strict_json(outcomes)
    require(isinstance(outcomes, list) and len(outcomes) == 2
            and all(isinstance(value, str) for value in outcomes)
            and {value.lower() for value in outcomes} == {'up', 'down'}, 'BTC_UP_DOWN_OUTCOMES_REQUIRED')
    for trade in selection['observed_trades']:
        require(tokens[trade['outcome_index']] == trade['token_id'], 'TRADE_GAMMA_TOKEN_CONFLICT')
    return {'version': version, 'condition_id': selection['condition_id'], 'slug': selection['slug'],
            'token_ids': tokens, 'outcomes': outcomes,
            'token_field': 'positionIds' if version == 'v2' else 'clobTokenIds'}


def validate_clob_binding(document, binding):
    # The compact endpoint identifies the requested condition in the URL.
    # Its documented token list provides an independent cross-source binding.
    tokens = document.get('t')
    require(isinstance(tokens, list) and len(tokens) == 2 and all(isinstance(item, dict) for item in tokens),
            'CLOB_TOKEN_ARRAY_REQUIRED')
    observed = {token_id(item.get('t')): item.get('o') for item in tokens}
    expected = dict(zip(binding['token_ids'], binding['outcomes']))
    require(len(observed) == 2 and observed == expected, 'CLOB_GAMMA_TOKEN_CONFLICT')


def validate_book_binding(document, binding, token):
    require(condition_id(document.get('market')) == binding['condition_id']
            and token_id(document.get('asset_id')) == token, 'BOOK_IDENTITY_CONFLICT')


def field(document, path, request_index, kind='number', unit=None, positive=False, maximum=None, basis=None):
    value = document
    present = True
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            present, value = False, None
            break
        value = value[key]
    result = {'value': None, 'unit': unit, 'state': 'missing' if not present else 'null',
              'source_request_index': request_index, 'source_json_path': '$.' + path,
              'unit_basis': basis}
    if value is None:
        return result
    if kind == 'boolean':
        if type(value) is bool:
            result.update(value=value, state='observed')
        else:
            result['state'] = 'invalid_type'
        return result
    if kind == 'string':
        if isinstance(value, str):
            result.update(value=value, state='observed')
        else:
            result['state'] = 'invalid_type'
        return result
    try:
        require(type(value) in ((str,) if kind == 'numeric_string' else (int, float, Decimal)), 'INVALID_NUMBER_TYPE')
        number = Decimal(str(value))
        require(number.is_finite() and (number > 0 if positive else number >= 0)
                and (maximum is None or number <= maximum), 'INVALID_NUMBER')
    except (ValueError, InvalidOperation):
        result['state'] = 'invalid_number'
        return result
    result.update(value=str(number), state='observed')
    return result


def assessment(selection, source_indices, records, finished_ms, stopped_reason=None):
    """Keep evidence provenance and incompatibilities; never certify an order."""
    result = {'version': 1, 'scope': 'public_market_constraint_evidence_only', 'selection': selection,
              'finished_ms': finished_ms, 'stopped_reason': stopped_reason,
              'minimum_notional_usd': None, 'minimum_notional_verified': False,
              'notional_status': 'unresolved_documentation_contract',
              'order_acceptance_observed': False, 'strategy_evaluated': False,
              'current_execution_constraints_certified': False,
              'source_request_indices': source_indices, 'identity_conflicts': [],
              'fee_comparison': {}, 'tick_comparison': {},
              'comparison_interpretation': 'sequential_observed_values; differences_may_reflect_changes_between_receipts',
              'gamma': None, 'clob': None, 'books': {}}
    if selection.get('status') != 'selected':
        result['interpretation'] = ('receipt_clock_order_unverified' if selection.get('status') == 'receipt_clock_order_unverified'
                                    else 'no_current_market_selected')
        result['receipt_clock_order_consistent'] = (
            selection.get('status') != 'receipt_clock_order_unverified' and clock_sequence_valid(records, finished_ms))
        return result
    start, end = selection['window_start_ms'], selection['window_end_ms']
    result['interpretation'] = 'within_selected_market_window' if start <= finished_ms < end else 'historical_evidence_only'
    binding = None
    try:
        index = source_indices['gamma']
        document = record_document(records[index])
        binding = gamma_binding(document, selection)
        result['gamma'] = {
            'binding': binding,
            'order_min_size': field(document, 'orderMinSize', index, positive=True),
            'tick_size': field(document, 'orderPriceMinTickSize', index, positive=True, maximum=1),
            'fees_enabled': field(document, 'feesEnabled', index, kind='boolean'),
            'fee_rate': field(document, 'feeSchedule.rate', index, maximum=1),
            'fee_exponent': field(document, 'feeSchedule.exponent', index),
            'fee_taker_only': field(document, 'feeSchedule.takerOnly', index, kind='boolean'),
            'active': field(document, 'active', index, kind='boolean'),
            'closed': field(document, 'closed', index, kind='boolean'),
            'accepting_orders': field(document, 'acceptingOrders', index, kind='boolean')}
        result['gamma']['order_min_size'].update(
            documented_unit='USDC notional', documented_unit_unresolved=True,
            documentation_basis=GAMMA_CONSTRAINT_DOC,
            interpretation='documented_notional_description_is_not_a_verified_order_acceptance_floor')
    except (ValueError, TypeError, KeyError):
        result['identity_conflicts'].append('gamma_unavailable_or_identity_unverified')
    if binding:
        try:
            index = source_indices['clob']
            document = record_document(records[index])
            validate_clob_binding(document, binding)
            result['clob'] = {'minimum_order_size': field(document, 'mos', index, positive=True),
                              'tick_size': field(document, 'mts', index, positive=True, maximum=1),
                              'fee_rate': field(document, 'fd.r', index, maximum=1),
                              'fee_exponent': field(document, 'fd.e', index),
                              'fee_taker_only': field(document, 'fd.to', index, kind='boolean'),
                              'maker_base_fee': field(document, 'mbf', index, unit='basis_points'),
                              'taker_base_fee': field(document, 'tbf', index, unit='basis_points')}
        except (ValueError, TypeError, KeyError):
            result['identity_conflicts'].append('clob_unavailable_or_identity_unverified')
        for token, outcome in zip(binding['token_ids'], binding['outcomes']):
            try:
                index = source_indices.get('books', {})[token]
                document = record_document(records[index])
                validate_book_binding(document, binding, token)
                result['books'][outcome] = {
                    'token_id': token,
                    'minimum_order_size': field(document, 'min_order_size', index, kind='numeric_string', unit='shares', positive=True, basis=BOOK_DOC),
                    'tick_size': field(document, 'tick_size', index, kind='numeric_string', positive=True, maximum=1),
                    'exchange_timestamp_raw': field(document, 'timestamp', index, kind='string'),
                    'exchange_timestamp_unit': None,
                    'order_size_step': None, 'order_size_step_verified': False}
            except (ValueError, TypeError, KeyError):
                result['identity_conflicts'].append('book_unavailable_or_identity_unverified:' + outcome)
        if result['gamma'] and result['clob']:
            for key in ('fee_rate', 'fee_exponent', 'fee_taker_only'):
                left, right = result['gamma'][key], result['clob'][key]
                if left['state'] == right['state'] == 'observed':
                    equal = left['value'] == right['value'] if key == 'fee_taker_only' else Decimal(left['value']) == Decimal(right['value'])
                    result['fee_comparison'][key] = 'equal_observed_values' if equal else 'conflicting_observed_values'
                else:
                    result['fee_comparison'][key] = 'unknown'
            enabled, rate = result['gamma']['fees_enabled'], result['clob']['fee_rate']
            result['fee_comparison']['enabled_vs_rate'] = (
                'conflicting_observed_values' if enabled['state'] == rate['state'] == 'observed'
                and enabled['value'] is False and Decimal(rate['value']) > 0 else 'not_established')
        ticks = {}
        for name in ('gamma', 'clob'):
            if result[name]:
                ticks[name] = result[name]['tick_size']
        ticks.update({'book_' + outcome: values['tick_size'] for outcome, values in result['books'].items()})
        names = sorted(ticks)
        for offset, left_name in enumerate(names):
            for right_name in names[offset + 1:]:
                left, right = ticks[left_name], ticks[right_name]
                comparison = 'unknown'
                if left['state'] == right['state'] == 'observed':
                    comparison = ('equal_observed_values' if Decimal(left['value']) == Decimal(right['value'])
                                  else 'conflicting_observed_values')
                result['tick_comparison'][left_name + '_vs_' + right_name] = {
                    'comparison': comparison, 'left_source_request_index': left['source_request_index'],
                    'right_source_request_index': right['source_request_index']}
    relevant = [records[index] for name, index in source_indices.items() if name != 'books']
    relevant += [records[index] for index in source_indices.get('books', {}).values()]
    prefix_count = selection['selection_source_request_count']
    selected_at = selection['selected_at_ms']
    result['receipt_clock_order_consistent'] = (
        clock_sequence_valid(records, finished_ms)
        and clock_sequence_valid(records[:prefix_count], selected_at)
        and all(record['request_started_ms'] >= selected_at for record in records[prefix_count:]))
    if not result['receipt_clock_order_consistent']:
        result['interpretation'] = 'receipt_clock_order_unverified'
    elif any(not start <= record['finished_ms'] < end for record in relevant):
        result['interpretation'] = 'historical_evidence_only'
    return result
