"""Offline Data API contract checks using synthetic pages and stdlib only.

The in-memory vault adapter exposes synthetic records for assertions; it does
not test encryption or read identities. No production CLI or trading module is
loaded. Socket connection attempts fail throughout import and every test.
"""
import importlib.util
import json
from pathlib import Path
import socket
import types
import unittest
from unittest.mock import patch


def deny_network(*args, **kwargs):
    raise AssertionError('NETWORK_FORBIDDEN_IN_OFFLINE_CONTRACT_TEST')


def load_json_bytes(body, cap):
    if not isinstance(body, bytes) or len(body) > cap:
        raise ValueError('INVALID_SYNTHETIC_BODY')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('OBJECT_REQUIRED')
    return value


def validate_recipient(recipient, fingerprint):
    if recipient != {'fixture': True} or fingerprint != 'synthetic-recipient':
        raise ValueError('INVALID_SYNTHETIC_RECIPIENT')


def deny_file_access(*args, **kwargs):
    raise AssertionError('IDENTITY_FILE_ACCESS_FORBIDDEN')


def load_feed():
    vault = types.ModuleType('research_vault')
    vault.canonical = lambda value: json.dumps(value, sort_keys=True).encode()
    vault.load_json_bytes = load_json_bytes
    vault.validate_recipient = validate_recipient
    vault.seal = lambda body, recipient, fingerprint: body
    vault.read_bounded = deny_file_access
    vault.write_new = deny_file_access
    source = Path(__file__).resolve().parents[1] / 'tools' / 'sealed_public_feed.py'
    spec = importlib.util.spec_from_file_location('offline_sealed_public_feed', source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', {'research_vault': vault}), \
            patch.object(socket.socket, 'connect', deny_network), \
            patch.object(socket.socket, 'connect_ex', deny_network), \
            patch.object(socket, 'create_connection', deny_network):
        spec.loader.exec_module(module)
    return module


feed = load_feed()


def page(rows, cursor=None):
    return json.dumps({'data': rows, 'pagination': {
        'has_more': cursor is not None, 'next_cursor': cursor,
        'limit': 10, 'offset': 999}}).encode()


def request(endpoint='/v2/trades', filters=None, max_pages=3):
    return {'endpoint': endpoint,
            'filters': {'user': 'synthetic-wallet', 'start': 1, 'end': 9, 'limit': 10}
            if filters is None else filters,
            'max_pages': max_pages}


class DataApiV2Contract(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.create_connection'):
            guard = patch(target, deny_network)
            guard.start()
            self.addCleanup(guard.stop)

    def collect_record(self, req, fetcher):
        ticks = iter(range(100, 200))
        body = feed.collect(req, {'fixture': True}, 'synthetic-recipient',
                            fetcher=fetcher, clock=lambda: next(ticks))
        return json.loads(body)

    def test_empty_and_short_pages_with_cursor_are_valid(self):
        for rows in ([], [{'size': None, 'price': None}]):
            with self.subTest(rows=rows):
                self.assertEqual(feed.parse_page(page(rows, 'opaque/+?=')),
                                 (rows, 'opaque/+?=', True))

    def test_null_cursor_terminates_empty_page(self):
        self.assertEqual(feed.parse_page(page([])), ([], None, False))

    def test_inconsistent_pagination_rejected(self):
        for more, cursor in ((True, None), (True, ''), (True, 12),
                             (True, 'x' * 8193), (False, 'next'), (0, None)):
            with self.subTest(more=more, cursor_type=type(cursor).__name__):
                body = json.dumps({'data': [], 'pagination': {
                    'has_more': more, 'next_cursor': cursor}}).encode()
                with self.assertRaises(ValueError):
                    feed.parse_page(body)

    def test_feed_null_data_is_not_silently_coerced_to_empty_list(self):
        with self.assertRaises(ValueError):
            feed.parse_page(b'{"data":null,"pagination":{"has_more":false,"next_cursor":null}}')

    def test_empty_then_short_page_replays_filters_until_null_cursor(self):
        for endpoint in ('/v2/trades', '/v2/activity'):
            with self.subTest(endpoint=endpoint):
                req = request(endpoint)
                calls = []
                replies = iter((page([], 'opaque/+?='),
                                page([{'size': None, 'price': None}], 'second'), page([])))
                def fetch(ep, query):
                    self.assertEqual(ep, endpoint)
                    calls.append(query)
                    return 200, next(replies)
                record = self.collect_record(req, fetch)
                self.assertEqual(calls, [req['filters'], dict(req['filters'], cursor='opaque/+?='),
                                         dict(req['filters'], cursor='second')])
                self.assertEqual(len(record['observed_pages']), 3)
                self.assertEqual(record['completion'], 'CURSOR_EXHAUSTED_FOR_REQUEST')
                self.assertFalse(record['all_history_complete'])
                self.assertFalse(record['orders_enabled'])

    def test_repeated_cursor_on_empty_pages_stops_without_extra_request(self):
        calls = []
        def fetch(endpoint, query):
            calls.append(query)
            return 200, page([], 'same')
        record = self.collect_record(request(max_pages=5), fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(record['completion'], 'VALIDATION_OR_TRANSPORT_STOP')
        self.assertFalse(record['all_history_complete'])

    def test_nonconsecutive_cursor_loop_stops(self):
        calls = []
        cursors = iter(('first', 'second', 'first'))
        def fetch(endpoint, query):
            calls.append(query)
            return 200, page([], next(cursors))
        record = self.collect_record(request(max_pages=5), fetch)
        self.assertEqual(len(calls), 3)
        self.assertEqual(record['completion'], 'VALIDATION_OR_TRANSPORT_STOP')

    def test_empty_progress_pages_respect_page_budget(self):
        calls = []
        def fetch(endpoint, query):
            calls.append(query)
            return 200, page([], f'cursor-{len(calls)}')
        record = self.collect_record(request(max_pages=2), fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(record['completion'], 'BUDGET_STOP')
        self.assertFalse(record['all_history_complete'])

    def test_cursor_is_opaque_and_does_not_mutate_filters(self):
        filters = request()['filters']
        cursor = 'not-json/+?=:%'
        self.assertEqual(feed.next_query(filters, cursor), dict(filters, cursor=cursor))
        self.assertNotIn('cursor', filters)

    def test_offset_is_display_metadata_only(self):
        for endpoint in ('/v2/trades', '/v2/activity'):
            with self.assertRaises(ValueError):
                feed.validate_request(request(endpoint, {'user': 'synthetic', 'offset': 1}))
        self.assertEqual(feed.parse_page(page([], 'next'))[1], 'next')

    def test_cross_endpoint_filters_rejected(self):
        for endpoint, extra in (('/v2/trades', {'sort_direction': 'ASC'}),
                                ('/v2/trades', {'type': 'TRADE'}),
                                ('/v2/activity', {'taker_only': True}),
                                ('/v2/activity', {'filter_type': 'TOKENS'}),
                                ('/v2/activity', {'filter_amount': 1})):
            with self.subTest(endpoint=endpoint, extra=extra):
                with self.assertRaises(ValueError):
                    feed.validate_request(request(endpoint, dict(user='synthetic', **extra)))

    def test_documented_activity_filters_preserved(self):
        filters = {'user': 'synthetic', 'type': 'TRADE,REDEEM', 'sort_by': 'TIMESTAMP',
                   'sort_direction': 'ASC', 'exclude_deposits_withdrawals': False}
        self.assertEqual(feed.validate_request(request('/v2/activity', filters))[1], filters)
        self.assertEqual(feed.next_query(filters, 'next'), dict(filters, cursor='next'))

    def test_activity_value_sorts_and_invalid_direction_rejected(self):
        for extra in ({'sort_by': 'CASH'}, {'sort_by': 'TOKENS'}, {'sort_direction': 'SIDEWAYS'}):
            with self.assertRaises(ValueError):
                feed.validate_request(request('/v2/activity', dict(user='synthetic', **extra)))

    def test_activity_condition_and_event_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            feed.validate_request(request('/v2/activity', {
                'user': 'synthetic', 'condition': '0x' + '1' * 64, 'event_id': '1'}))

    def test_identifier_lists_allow_twenty_distinct_and_duplicates(self):
        for endpoint in ('/v2/trades', '/v2/activity'):
            for field in ('condition', 'event_id'):
                ids = ['0x' + f'{i:064x}' if field == 'condition' else str(i) for i in range(20)]
                filters = {'user': 'synthetic', field: ','.join(ids + [ids[0]])}
                with self.subTest(endpoint=endpoint, field=field):
                    self.assertEqual(feed.validate_request(request(endpoint, filters))[1], filters)

    def test_identifier_lists_over_twenty_or_with_empty_values_rejected(self):
        for endpoint in ('/v2/trades', '/v2/activity'):
            for field in ('condition', 'event_id'):
                ids = ['0x' + f'{i:064x}' if field == 'condition' else str(i) for i in range(21)]
                for value in (','.join(ids), '1,,2', ''):
                    with self.subTest(endpoint=endpoint, field=field, length=len(value)):
                        with self.assertRaises(ValueError):
                            feed.validate_request(request(endpoint, {'user': 'synthetic', field: value}))

    def test_nonuser_trade_time_bounds_rejected(self):
        for field in ('start', 'end'):
            with self.assertRaises(ValueError):
                feed.validate_request(request(filters={'condition': '0x' + '1' * 64, field: 1}))
        self.assertEqual(feed.validate_request(request(filters={'limit': 10}))[1], {'limit': 10})

    def test_null_and_missing_numeric_fields_remain_unknown(self):
        rows = [{'size': None, 'price': None, 'outcome_index': 999}, {}]
        self.assertEqual(feed.parse_page(page(rows))[0], rows)
        for size, price in ((None, None), (None, '.5'), ('10', None)):
            self.assertIsNone(feed.market_units(size, price)['gross_notional_usdc'])
            self.assertFalse(feed.market_units(size, price)['pnl_known'])

    def test_http_refusals_stop_once_without_fallback(self):
        for status in (400, 403, 429):
            calls = []
            def fetch(endpoint, query):
                calls.append((endpoint, query))
                return status, b'{"error":"synthetic refusal"}'
            record = self.collect_record(request(), fetch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(record['completion'], 'HTTP_STOP')
            self.assertEqual(record['http_status'], status)
            self.assertEqual(record['observed_pages'], [])

    def test_socket_guard_rejects_attempted_network(self):
        with self.assertRaisesRegex(AssertionError, 'NETWORK_FORBIDDEN'):
            socket.create_connection(('example.invalid', 443))


if __name__ == '__main__':
    unittest.main()
