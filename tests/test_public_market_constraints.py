"""Synthetic fixtures follow the cited public API field names and types."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import public_market_constraints as constraints

START = 1_800_000_000_000
SLUG = 'btc-updown-5m-1800000000'
CONDITION = '0x' + 'a' * 64
TOKENS = ['1234', '5678']


def trade(**changes):
    return {'slug': SLUG, 'condition_id': CONDITION, 'token_id': TOKENS[0], 'outcome_index': 0, **changes}


def gamma(**changes):
    # Gamma market details: outcomes/clobTokenIds JSON strings; positionIds array.
    return {'slug': SLUG, 'conditionId': CONDITION, 'version': 'v2',
            'positionIds': TOKENS[:], 'clobTokenIds': '["999","888"]', 'outcomes': '["Up","Down"]',
            'orderMinSize': 5, 'orderPriceMinTickSize': 0.01,
            'active': True, 'closed': False, 'acceptingOrders': True, 'feesEnabled': True,
            'feeSchedule': {'rate': 0.07, 'exponent': 1, 'takerOnly': True, 'rebateRate': 0.2}, **changes}


def clob(**changes):
    # Compact CLOB market info has no root condition_id field.
    return {'mos': 5, 'mts': 0.01, 't': [{'t': TOKENS[0], 'o': 'Up'}, {'t': TOKENS[1], 'o': 'Down'}],
            'fd': {'r': 0.07, 'e': 1, 'to': True}, 'mbf': 0, 'tbf': 700, **changes}


def book(token=TOKENS[0], **changes):
    return {'market': CONDITION, 'asset_id': token, 'timestamp': '1800000010000', 'hash': 'fixture',
            'bids': [{'price': '0.49', 'size': '100'}], 'asks': [{'price': '0.50', 'size': '100'}],
            'min_order_size': '5', 'tick_size': '0.01', 'neg_risk': False, 'last_trade_price': '0.5', **changes}


def record(document, endpoint='/v2/trades', ms=START + 10_000):
    return {'endpoint': endpoint, 'http_status': 200, 'exception_code': None,
            'body_exceeded_cap': False, 'raw_body_base64': base64.b64encode(json.dumps(document).encode()).decode(),
            'request_started_ms': ms - 10, 'received_ms': ms, 'finished_ms': ms + 10,
            'monotonic_elapsed_ns': 20_000_000}


def trade_record(rows=None):
    return record({'data': [trade()] if rows is None else rows,
                   'pagination': {'has_more': False, 'next_cursor': None}})


def sources(gamma_document=None, clob_document=None, first_book=None):
    records = [trade_record(), record(gamma() if gamma_document is None else gamma_document, '/markets/slug/' + SLUG, START + 30_000),
               record(clob() if clob_document is None else clob_document, '/clob-markets/' + CONDITION, START + 31_000),
               record(book() if first_book is None else first_book, '/book', START + 32_000),
               record(book(TOKENS[1]), '/book', START + 33_000)]
    selection = constraints.select_current_market(records[:1], START + 20_000)
    indices = {'gamma': 1, 'clob': 2, 'books': {TOKENS[0]: 3, TOKENS[1]: 4}}
    return selection, indices, records


class PublicMarketConstraintTests(unittest.TestCase):
    def test_selects_current_identity_only_ignoring_price_profit_and_previous_window(self):
        previous = trade(slug='btc-updown-5m-1799999700', price=0.001, pnl=100000)
        result = constraints.select_current_market([trade_record([previous, trade(price=0.99, pnl=-50)])], START + 20_000)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['slug'], SLUG)
        self.assertEqual(result['observed_trades'][0]['source_row_index'], 1)
        self.assertNotIn('price', result['observed_trades'][0])
        expired = constraints.select_current_market([trade_record()], START + 300_000)
        self.assertEqual(expired['status'], 'no_current_btc5m_in_observed_prefix')

    def test_conflicting_current_conditions_or_token_indices_do_not_choose_one(self):
        cases = [[trade(), trade(condition_id='0x' + 'b' * 64)],
                 [trade(), trade(outcome_index=1)], [trade(), trade(token_id=TOKENS[1])],
                 [trade(), trade(condition_id=None)], [trade(outcome_index=True)]]
        for rows in cases:
            with self.subTest(rows=rows):
                result = constraints.select_current_market([trade_record(rows)], START + 20_000)
                self.assertEqual(result['status'], 'ambiguous_or_invalid_current_identity')

    def test_v2_trade_identity_does_not_fall_back_to_v1_aliases(self):
        row = {'slug': SLUG, 'conditionId': CONDITION, 'asset': TOKENS[0], 'outcome_index': 0}
        result = constraints.select_current_market([trade_record([row])], START + 20_000)
        self.assertEqual(result['status'], 'ambiguous_or_invalid_current_identity')

    def test_invalid_v2_envelope_cannot_seed_a_market_lookup(self):
        for document in [{'data': [trade()]},
                         {'data': [trade()], 'pagination': {'has_more': True, 'next_cursor': None}},
                         {'data': [trade()], 'pagination': {'has_more': False, 'next_cursor': 'cursor'}}]:
            with self.subTest(document=document):
                result = constraints.select_current_market([record(document)], START + 20_000)
                self.assertEqual(result['status'], 'no_current_btc5m_in_observed_prefix')
                self.assertEqual(result['unusable_trade_responses'], 1)

    def test_strict_identifier_validation_rejects_urls_paths_bools_and_uint256_overflow(self):
        for value in ['1/../../orders', 'https://example.org', True, '01', str(2 ** 256), '0']:
            with self.subTest(token=value), self.assertRaises(ValueError):
                constraints.token_id(value)
        for value in [CONDITION + '/orders', '0x123', False]:
            with self.subTest(condition=value), self.assertRaises(ValueError):
                constraints.condition_id(value)
        for value in [SLUG + '?user=x', 'btc-updown-5m-1800000001', 'eth-updown-5m-1800000000']:
            with self.subTest(slug=value), self.assertRaises(ValueError):
                constraints.slug_start_ms(value)

    def test_version_selects_token_array_even_when_both_arrays_are_present(self):
        selection = constraints.select_current_market([trade_record()], START + 20_000)
        self.assertEqual(constraints.gamma_binding(gamma(), selection)['token_field'], 'positionIds')
        v1 = gamma(version='v1', clobTokenIds=json.dumps(TOKENS), positionIds=['999', '888'])
        self.assertEqual(constraints.gamma_binding(v1, selection)['token_ids'], TOKENS)
        for version in [None, 1, 2, 'v3', True]:
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'GAMMA_VERSION_UNKNOWN'):
                constraints.gamma_binding(gamma(version=version), selection)
        with self.assertRaises(ValueError):
            constraints.gamma_binding(gamma(version='v1', clobTokenIds=TOKENS), selection)
        with self.assertRaises(ValueError):
            constraints.gamma_binding(gamma(positionIds=json.dumps(TOKENS)), selection)

    def test_every_layer_binds_condition_token_and_outcome(self):
        selection = constraints.select_current_market([trade_record()], START + 20_000)
        with self.assertRaisesRegex(ValueError, 'TRADE_GAMMA_TOKEN_CONFLICT'):
            constraints.gamma_binding(gamma(positionIds=TOKENS[::-1]), selection)
        binding = constraints.gamma_binding(gamma(), selection)
        with self.assertRaisesRegex(ValueError, 'CLOB_GAMMA_TOKEN_CONFLICT'):
            constraints.validate_clob_binding(clob(t=[{'t': TOKENS[0], 'o': 'Down'}, {'t': TOKENS[1], 'o': 'Up'}]), binding)
        with self.assertRaisesRegex(ValueError, 'BOOK_IDENTITY_CONFLICT'):
            constraints.validate_book_binding(book(market='0x' + 'b' * 64), binding, TOKENS[0])

    def test_constraint_units_sources_and_unknown_notional_remain_separate(self):
        selection, indices, records = sources()
        result = constraints.assessment(selection, indices, records, START + 50_000)
        self.assertEqual(result['identity_conflicts'], [])
        self.assertEqual(result['books']['Up']['minimum_order_size']['unit'], 'shares')
        self.assertEqual(result['books']['Up']['minimum_order_size']['source_json_path'], '$.min_order_size')
        self.assertIsNone(result['gamma']['order_min_size']['unit'])
        self.assertIsNone(result['clob']['minimum_order_size']['unit'])
        self.assertIsNone(result['minimum_notional_usd'])
        self.assertFalse(result['minimum_notional_verified'])
        self.assertFalse(result['order_acceptance_observed'])
        self.assertFalse(result['current_execution_constraints_certified'])
        self.assertEqual(result['books']['Up']['exchange_timestamp_raw']['value'], '1800000010000')
        self.assertIsNone(result['books']['Up']['exchange_timestamp_unit'])
        self.assertIsNone(result['books']['Up']['order_size_step'])

    def test_fee_conflict_never_replaces_one_source_with_the_other(self):
        selection, indices, records = sources(clob_document=clob(fd={'r': 0.25, 'e': 2, 'to': False}))
        result = constraints.assessment(selection, indices, records, START + 50_000)
        self.assertEqual(result['gamma']['fee_rate']['value'], '0.07')
        self.assertEqual(result['clob']['fee_rate']['value'], '0.25')
        for key in ['fee_rate', 'fee_exponent', 'fee_taker_only']:
            self.assertEqual(result['fee_comparison'][key], 'conflicting_observed_values')

    def test_documented_gamma_notional_and_sequential_tick_conflicts_are_explicit(self):
        selection, indices, records = sources(gamma_document=gamma(feesEnabled=False),
                                              first_book=book(tick_size='0.001'))
        result = constraints.assessment(selection, indices, records, START + 50_000)
        minimum = result['gamma']['order_min_size']
        self.assertIsNone(minimum['unit'])
        self.assertEqual(minimum['documented_unit'], 'USDC notional')
        self.assertTrue(minimum['documented_unit_unresolved'])
        self.assertIn('#trading-constraints', minimum['documentation_basis'])
        self.assertFalse(result['minimum_notional_verified'])
        self.assertEqual(result['fee_comparison']['enabled_vs_rate'], 'conflicting_observed_values')
        self.assertEqual(len(result['tick_comparison']), 6)
        self.assertEqual(result['tick_comparison']['book_Up_vs_gamma']['comparison'], 'conflicting_observed_values')
        self.assertEqual(result['tick_comparison']['clob_vs_gamma']['comparison'], 'equal_observed_values')
        self.assertIn('differences_may_reflect_changes_between_receipts', result['comparison_interpretation'])

    def test_missing_null_boolean_numeric_and_nan_never_become_zero_or_known(self):
        selection, indices, records = sources(gamma_document=gamma(feeSchedule=None, feesEnabled=False),
                                              clob_document=clob(fd={'r': True, 'e': None, 'to': 1}),
                                              first_book=book(min_order_size=True))
        result = constraints.assessment(selection, indices, records, START + 50_000)
        self.assertEqual(result['gamma']['fee_rate']['state'], 'missing')
        self.assertEqual(result['clob']['fee_rate']['state'], 'invalid_number')
        self.assertEqual(result['clob']['fee_exponent']['state'], 'null')
        self.assertEqual(result['clob']['fee_taker_only']['state'], 'invalid_type')
        self.assertIsNone(result['books']['Up']['minimum_order_size']['value'])
        for text in ['{"value":NaN}', '{"value":Infinity}', '{"value":1,"value":2}']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                constraints.strict_json(text)
        evidence = constraints.field({'rate': float('nan')}, 'rate', 0)
        self.assertEqual(evidence['state'], 'invalid_number')

    def test_expired_or_reversed_receipts_never_certify_current_constraints(self):
        selection, indices, records = sources()
        result = constraints.assessment(selection, indices, records, START + 300_000)
        self.assertEqual(result['interpretation'], 'historical_evidence_only')
        records[3]['received_ms'] = records[3]['request_started_ms'] - 1
        result = constraints.assessment(selection, indices, records, START + 50_000)
        self.assertEqual(result['interpretation'], 'receipt_clock_order_unverified')
        self.assertFalse(result['receipt_clock_order_consistent'])

    def test_future_or_reversed_trade_receipts_cannot_select_a_market(self):
        future = trade_record()
        future.update(request_started_ms=START + 59_000, received_ms=START + 60_000, finished_ms=START + 61_000)
        reversed_receipt = trade_record()
        reversed_receipt['received_ms'] = reversed_receipt['request_started_ms'] - 1
        boolean_clock = trade_record()
        boolean_clock['finished_ms'] = True
        for source in (future, reversed_receipt, boolean_clock):
            with self.subTest(source=source):
                selected = constraints.select_current_market([source], START + 20_000)
                self.assertEqual(selected['status'], 'receipt_clock_order_unverified')
                self.assertNotIn('condition_id', selected)
        selected = constraints.select_current_market([future], START + 20_000)
        later = constraints.assessment(selected, {}, [future], START + 70_000)
        self.assertEqual(later['interpretation'], 'receipt_clock_order_unverified')
        self.assertFalse(later['receipt_clock_order_consistent'])

    def test_lookup_before_selection_cross_request_reverse_and_early_final_are_unverified(self):
        for mutation in ('before_selection', 'cross_request', 'final_before_receipt'):
            selection, indices, records = sources()
            finished = START + 50_000
            if mutation == 'before_selection':
                records[1].update(request_started_ms=START + 15_000, received_ms=START + 16_000, finished_ms=START + 17_000)
            elif mutation == 'cross_request':
                records[2].update(request_started_ms=START + 29_000, received_ms=START + 31_000, finished_ms=START + 31_010)
            else:
                finished = START + 32_500
            with self.subTest(mutation=mutation):
                result = constraints.assessment(selection, indices, records, finished)
                self.assertEqual(result['interpretation'], 'receipt_clock_order_unverified')
                self.assertFalse(result['receipt_clock_order_consistent'])

    def test_book_identity_failure_retains_unavailable_side_instead_of_success(self):
        selection, indices, records = sources(first_book=book(asset_id='999'))
        result = constraints.assessment(selection, indices, records, START + 50_000)
        self.assertNotIn('Up', result['books'])
        self.assertIn('Down', result['books'])
        self.assertIn('book_unavailable_or_identity_unverified:Up', result['identity_conflicts'])


if __name__ == '__main__':
    unittest.main()
