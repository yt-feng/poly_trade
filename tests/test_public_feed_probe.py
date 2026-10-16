"""Only synthetic mocks: never call a public endpoint or use a real identity."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import persist_quality_first_journal as journal
import research_vault
import run_public_feed_probe as probe
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def page(rows=None, more=False, cursor=None):
    return json.dumps({'data': rows or [], 'pagination': {'has_more': more, 'next_cursor': cursor}}).encode()


class PublicFeedProbeTests(unittest.TestCase):
    def setUp(self):
        self.private = X25519PrivateKey.generate()
        self.recipient = research_vault.recipient_for(self.private)
        self.recipient_id = self.recipient['recipient_id']
        self.environment = {'GITHUB_ACTIONS': 'true',
                            'QUALITY_FIRST_RECIPIENT_JSON_B64': base64.b64encode(research_vault.canonical(self.recipient)).decode(),
                            'QUALITY_FIRST_RECIPIENT_ID': self.recipient_id}

    def recover(self, output):
        parts = []
        for index, path in enumerate(sorted(output.glob('part-*.vault'))):
            document = json.loads(research_vault.unseal(path.read_bytes(), self.private))
            self.assertEqual(document['index'], index)
            parts.append(base64.b64decode(document['payload_base64']))
        bundle = b''.join(parts)
        self.assertEqual(hashlib.sha256(bundle).hexdigest(), document['bundle_sha256'])
        with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
            return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}

    def run_probe(self, fetcher, collect=None, mode='availability'):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, self.environment), patch.object(probe, 'public_get', side_effect=fetcher) as http:
            output = Path(temporary) / 'delivery'
            public = io.StringIO()
            with contextlib.redirect_stdout(public), contextlib.redirect_stderr(public):
                if collect:
                    with patch.object(probe.sealed_public_feed, 'collect', side_effect=collect):
                        code = probe.run(argparse.Namespace(output=output, mode=mode))
                else:
                    code = probe.run(argparse.Namespace(output=output, mode=mode))
            status = json.loads((output / 'status.json').read_text())
            inspected = journal.inspect_delivery(output, self.recipient_id)
            self.assertEqual(inspected['status'], status)
            self.assertEqual(set(path.name for path in output.iterdir()),
                             {'status.json'} | {f'part-{index:04d}.vault' for index in range(status['sealed_parts'])})
            return code, status, self.recover(output), public.getvalue(), http.call_args_list

    def test_cloud_gate_blocks_local_network_even_with_valid_recipient(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {**self.environment, 'GITHUB_ACTIONS': 'false'}), \
             patch.object(probe, 'public_get') as fetch:
            output = Path(temporary) / 'delivery'
            with self.assertRaisesRegex(ValueError, 'GITHUB_ACTIONS_REQUIRED'):
                probe.run(argparse.Namespace(output=output))
            fetch.assert_not_called()
            self.assertFalse(output.exists())

    def test_transport_itself_refuses_noncloud_execution(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': ''}), patch.object(probe, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'GITHUB_ACTIONS_REQUIRED'):
                probe.public_get(probe.STATUS_ENDPOINT, {})
            opener.assert_not_called()

    def test_missing_invalid_or_mismatched_recipient_prevents_every_request(self):
        for mutation in ({'QUALITY_FIRST_RECIPIENT_JSON_B64': ''},
                         {'QUALITY_FIRST_RECIPIENT_JSON_B64': 'not base64'},
                         {'QUALITY_FIRST_RECIPIENT_ID': '0' * 64}):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary, \
                 patch.dict(os.environ, {**self.environment, **mutation}), patch.object(probe, 'public_get') as fetch:
                output = Path(temporary) / 'delivery'
                with self.assertRaises(Exception):
                    probe.run(argparse.Namespace(output=output))
                fetch.assert_not_called()
                self.assertFalse(output.exists())

    def test_fixed_two_page_walk_status_brackets_and_nested_envelope_round_trip(self):
        marker = 'SYNTHETIC_RAW_BODY_ONLY_IN_CIPHERTEXT'
        responses = iter([(200, json.dumps({'data': {'marker': marker}}).encode()),
                          (200, page(more=True, cursor='next-private-cursor')),
                          (200, page([{'fixture': marker}])), (200, b'{"data":null}')])
        code, status, members, public, calls = self.run_probe(lambda *args: next(responses))
        self.assertEqual(code, 0)
        self.assertEqual(status['status'], 'complete')
        self.assertFalse(status['strategy_evaluated'])
        self.assertNotIn(marker, public)
        self.assertNotIn('next-private-cursor', public)
        self.assertEqual([call.args[0] for call in calls],
                         ['/v2/status', '/v2/trades', '/v2/trades', '/v2/status'])
        self.assertEqual([call.args[1] for call in calls],
                         [{}, {'limit': 100, 'taker_only': True},
                          {'limit': 100, 'taker_only': True, 'cursor': 'next-private-cursor'}, {}])
        observations = json.loads(members['results/http-observations.json'])
        self.assertEqual(len(observations['records']), 4)
        for record in observations['records']:
            self.assertIsInstance(record['received_ms'], int)
            self.assertIsNotNone(record['received_utc'])
            self.assertGreaterEqual(record['finished_ms'], record['request_started_ms'])
            self.assertGreaterEqual(record['monotonic_elapsed_ns'], 0)
            self.assertIsNone(record['exception_code'])
            body = base64.b64decode(record['raw_body_base64'])
            self.assertEqual(record['retained_body_sha256'], hashlib.sha256(body).hexdigest())
        self.assertFalse(observations['status_schema_assessed'])
        self.assertFalse(observations['freshness_assessed'])
        inner = json.loads(research_vault.unseal(members['results/public-feed.vault'], self.private))
        self.assertEqual(inner['completion'], 'CURSOR_EXHAUSTED_FOR_REQUEST')
        self.assertEqual(len(inner['observed_pages']), 2)
        self.assertFalse(inner['all_history_complete'])
        self.assertEqual(json.loads(members['run.json'])['request'], probe.FIXED_REQUEST)

    def test_http_refusals_are_sealed_evidence_not_public_service_or_freshness_claims(self):
        responses = iter([(503, b'SYNTHETIC_NOT_READY'), (429, b'SYNTHETIC_RATE_REFUSAL'),
                          (200, b'{"data":{"ingestion":{"lagging":[]},"serving":null}}')])
        code, status, members, public, calls = self.run_probe(lambda *args: next(responses))
        self.assertEqual(code, 0)  # Evidence was delivered; availability was not asserted.
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(status), journal.STATUS_KEYS)
        self.assertFalse(status['strategy_evaluated'])
        self.assertNotIn('503', public)
        self.assertNotIn('429', public)
        self.assertNotIn('SYNTHETIC_', public)
        observations = json.loads(members['results/http-observations.json'])
        self.assertEqual([record['http_status'] for record in observations['records']], [503, 429, 200])
        self.assertTrue(all(record['received_ms'] is not None for record in observations['records']))
        self.assertEqual(base64.b64decode(observations['records'][1]['raw_body_base64']), b'SYNTHETIC_RATE_REFUSAL')
        inner = json.loads(research_vault.unseal(members['results/public-feed.vault'], self.private))
        self.assertEqual(inner['completion'], 'HTTP_STOP')

    def test_transport_failure_has_no_invented_receipt_and_records_only_finite_exception_codes(self):
        count = 0
        def fetch(*args):
            nonlocal count
            count += 1
            if count <= 2:
                raise TimeoutError('SYNTHETIC_EXCEPTION_TEXT_NEVER_RETAINED')
            return 200, b'{"data":null}'
        code, status, members, public, calls = self.run_probe(fetch)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        records = json.loads(members['results/http-observations.json'])['records']
        for record in records[:2]:
            self.assertIsNone(record['received_ms'])
            self.assertIsNone(record['received_utc'])
            self.assertIsNone(record['http_status'])
            self.assertIsNone(record['raw_body_base64'])
            self.assertEqual(record['exception_code'], 'TIMEOUT')
            self.assertIsInstance(record['finished_ms'], int)
        self.assertNotIn('SYNTHETIC_EXCEPTION_TEXT', public)
        self.assertFalse(any(b'SYNTHETIC_EXCEPTION_TEXT' in body for body in members.values()))

    def test_collection_exception_still_observes_after_status_and_delivers_failed_sealed_evidence(self):
        code, status, members, public, calls = self.run_probe(
            lambda *args: (200, b'{"data":null}'),
            collect=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('SYNTHETIC_PRIVATE_EXCEPTION')))
        self.assertEqual(code, 2)
        self.assertEqual(status['failed_stage'], 'acquisition')
        self.assertEqual([call.args[0] for call in calls], ['/v2/status', '/v2/status'])
        self.assertNotIn('results/public-feed.vault', members)
        self.assertEqual(json.loads(members['run.json'])['collection_exception_code'], 'CLIENT_EXCEPTION')
        self.assertNotIn('SYNTHETIC_PRIVATE_EXCEPTION', public)

    def test_monotonic_elapsed_is_independent_of_reversed_wall_clock(self):
        recorder = probe.HTTPRecorder(fetcher=lambda *args: (200, b'raw'),
                                      clock=iter([5000, 4000, 3000]).__next__,
                                      monotonic=iter([100, 800]).__next__)
        recorder('/v2/status', {})
        record = recorder.records[0]
        self.assertEqual(record['monotonic_elapsed_ns'], 700)
        self.assertEqual((record['request_started_ms'], record['received_ms'], record['finished_ms']), (5000, 4000, 3000))

    def test_record_body_and_request_budgets_are_bounded_without_truncating_into_an_accepted_page(self):
        recorder = probe.HTTPRecorder(fetcher=lambda *args: (200, b'abcdef'))
        with patch.object(probe, 'BODY_LIMIT', 5):
            for _ in range(4):
                with self.assertRaises(ValueError):
                    recorder('/v2/status', {})
            with self.assertRaisesRegex(ValueError, 'REQUEST_BUDGET'):
                recorder('/v2/status', {})
        self.assertEqual(len(recorder.records), 4)
        for record in recorder.records:
            self.assertEqual(base64.b64decode(record['raw_body_base64']), b'abcde')
            self.assertTrue(record['body_exceeded_cap'])
            self.assertEqual(record['body_bytes_read'], 6)
            self.assertIsNotNone(record['received_ms'])

    def test_wallet_filters_alternative_endpoints_and_query_options_are_rejected(self):
        for endpoint, query in [('/v2/activity', {'user': 'some-address'}),
                                ('/v2/status', {'ignored': True}),
                                ('/v2/trades', {'limit': 100, 'taker_only': True, 'user': 'some-address'}),
                                ('/v2/trades', {'limit': 101, 'taker_only': True}),
                                ('/v2/trades', {'limit': 100, 'taker_only': False}),
                                ('/v2/trades', {'limit': 100, 'taker_only': True, 'cursor': ''})]:
            with self.subTest(endpoint=endpoint, query=query):
                with self.assertRaises(ValueError):
                    probe.validate_http_request(endpoint, query)

    def test_standard_transport_retains_environment_handlers_and_bounds_read_timeout(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.read.return_value = 200, b'{}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, self.environment), patch.object(probe, 'build_opener', return_value=opener) as build, \
             patch.object(probe.signal, 'signal'), patch.object(probe.signal, 'setitimer', return_value=(0., 0.)) as timer:
            self.assertEqual(probe.public_get('/v2/status', {}), (200, b'{}'))
        self.assertEqual(len(build.call_args.args), 1)
        self.assertIsInstance(build.call_args.args[0], probe.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://data-api.polymarket.com/v2/status')
        self.assertEqual(request.get_method(), 'GET')
        self.assertNotIn('Authorization', request.headers)
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 8})
        response.read.assert_called_once_with(probe.BODY_LIMIT + 1)
        self.assertEqual(timer.call_args_list[0].args, (probe.signal.ITIMER_REAL, 8))

    def test_http_error_body_is_retained_boundedly_without_retry_or_fallback(self):
        error = HTTPError('https://data-api.polymarket.com/v2/status', 503, 'unready', {}, io.BytesIO(b'raw-error'))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.dict(os.environ, self.environment), patch.object(probe, 'build_opener', return_value=opener), \
             patch.object(probe.signal, 'signal'), patch.object(probe.signal, 'setitimer', return_value=(0., 0.)):
            self.assertEqual(probe.public_get('/v2/status', {}), (503, b'raw-error'))
        opener.open.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'REDIRECT_REFUSED'):
            probe.NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.invalid')

    def test_workflow_contract_is_manual_main_only_and_existing_publisher_can_receive_artifact(self):
        workflow = (ROOT / '.github/workflows/sealed-feed-probe.yml').read_text()
        publisher = (ROOT / '.github/workflows/sealed-journal-persist.yml').read_text()
        self.assertIn("if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn('needs: contract', workflow)
        self.assertIn('test_public_feed_probe.py', workflow)
        self.assertNotIn('schedule:', workflow)
        self.assertIn('probe_mode:', workflow)
        self.assertIn('options: [availability, market-constraints]', workflow)
        self.assertIn('default: availability', workflow)
        self.assertNotIn('recipient_json_b64:', workflow)
        self.assertNotIn('wallet:', workflow)
        self.assertNotIn('secrets.', workflow)
        self.assertNotIn('GH_TOKEN:', workflow)
        self.assertIn('vars.QUALITY_FIRST_RECIPIENT_JSON_B64', workflow)
        self.assertIn('quality-first-sealed-${{ github.run_id }}-${{ github.run_attempt }}', workflow)
        self.assertIn('quality-first-research, sealed-feed-probe]', publisher)
        self.assertIn('quality-first-sealed-${{ github.event.workflow_run.id }}-${{ github.event.workflow_run.run_attempt }}', publisher)

    def test_market_constraints_mode_is_bounded_and_seals_identity_bound_real_schema_fields(self):
        from test_public_market_constraints import START, SLUG, CONDITION, TOKENS, trade, gamma, clob, book
        responses = iter([(200, b'{"data":null}'), (200, page(more=True, cursor='next')),
                          (200, page([trade()])), (200, json.dumps(gamma()).encode()),
                          (200, json.dumps(clob()).encode()), (200, json.dumps(book()).encode()),
                          (200, json.dumps(book(TOKENS[1])).encode()), (200, b'{"data":null}')])
        with patch.object(probe.time, 'time_ns', return_value=(START + 20_000) * 1_000_000):
            code, status, members, public, calls = self.run_probe(lambda *args: next(responses), mode='market-constraints')
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 8)
        self.assertEqual([call.args[0] for call in calls],
                         ['/v2/status', '/v2/trades', '/v2/trades', '/markets/slug/' + SLUG,
                          '/clob-markets/' + CONDITION, '/book', '/book', '/v2/status'])
        self.assertEqual([call.args[1] for call in calls[5:7]], [{'token_id': token} for token in TOKENS])
        result = json.loads(members['results/market-constraints.json'])
        self.assertEqual(result['selection']['status'], 'selected')
        self.assertEqual(result['identity_conflicts'], [])
        self.assertFalse(result['minimum_notional_verified'])
        self.assertFalse(result['order_acceptance_observed'])
        self.assertEqual(result['books']['Up']['minimum_order_size']['unit'], 'shares')
        self.assertEqual(result['gamma']['binding']['token_field'], 'positionIds')
        self.assertEqual(result['gamma']['order_min_size']['documented_unit'], 'USDC notional')
        self.assertTrue(result['gamma']['order_min_size']['documented_unit_unresolved'])
        observations = json.loads(members['results/http-observations.json'])['records']
        self.assertEqual([row['host'] for row in observations],
                         [probe.HOST] * 3 + [probe.GAMMA_HOST] + [probe.CLOB_HOST] * 3 + [probe.HOST])
        self.assertFalse(status['strategy_evaluated'])
        self.assertNotIn(SLUG, public)
        self.assertNotIn(CONDITION, public)

    def test_no_current_market_or_unknown_version_has_no_fallback_requests(self):
        from test_public_market_constraints import START, trade, gamma
        for current in (False, True):
            responses = [(200, b'{"data":null}'), (200, page([trade()] if current else []))]
            if current:
                responses.append((200, json.dumps(gamma(version=None)).encode()))
            responses.append((200, b'{"data":null}'))
            with self.subTest(current=current), patch.object(probe.time, 'time_ns', return_value=(START + 20_000) * 1_000_000):
                iterator = iter(responses)
                code, _, members, _, calls = self.run_probe(lambda *args: next(iterator), mode='market-constraints')
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 4 if current else 3)
            self.assertFalse(any(call.args[0].startswith('/clob-markets/') or call.args[0] == '/book' for call in calls))
            result = json.loads(members['results/market-constraints.json'])
            self.assertFalse(result['order_acceptance_observed'])
            if current:
                self.assertEqual(result['stopped_reason'], 'market_identity_or_source_unverified')
            else:
                self.assertEqual(result['selection']['status'], 'no_current_btc5m_in_observed_prefix')

    def test_crossing_window_during_after_status_marks_constraints_historical(self):
        from test_public_market_constraints import START, TOKENS, trade, gamma, clob, book
        now = [START + 20_000]
        responses = iter([(200, b'{"data":null}'), (200, page([trade()])),
                          (200, json.dumps(gamma()).encode()), (200, json.dumps(clob()).encode()),
                          (200, json.dumps(book()).encode()), (200, json.dumps(book(TOKENS[1])).encode()),
                          (200, b'{"data":null}')])
        count = 0
        def fetch(*args):
            nonlocal count
            count += 1
            if count == 7:
                now[0] = START + 300_000
            return next(responses)
        with patch.object(probe.time, 'time_ns', side_effect=lambda: now[0] * 1_000_000):
            code, _, members, _, _ = self.run_probe(fetch, mode='market-constraints')
        self.assertEqual(code, 0)
        result = json.loads(members['results/market-constraints.json'])
        self.assertEqual(result['interpretation'], 'historical_evidence_only')
        self.assertFalse(result['current_execution_constraints_certified'])

    def test_static_three_host_routes_reject_injected_urls_and_availability_cannot_expand(self):
        from test_public_market_constraints import SLUG, CONDITION, TOKENS
        for endpoint, filters, host in [('/markets/slug/' + SLUG, {}, probe.GAMMA_HOST),
                                         ('/clob-markets/' + CONDITION, {}, probe.CLOB_HOST),
                                         ('/book', {'token_id': TOKENS[0]}, probe.CLOB_HOST)]:
            self.assertEqual(probe.validate_http_request(endpoint, filters), host)
            with self.assertRaisesRegex(ValueError, 'ENDPOINT_OUTSIDE_PROBE_MODE'):
                probe.HTTPRecorder(fetcher=lambda *args: (200, b'{}'))(endpoint, filters)
        for endpoint, filters in [('https://evil.invalid/v2/status', {}),
                                 ('/markets/slug/' + SLUG + '/orders', {}),
                                 ('/clob-markets/' + CONDITION + '?user=x', {}),
                                 ('/book', {'token_id': TOKENS[0], 'user': 'x'})]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                probe.validate_http_request(endpoint, filters)

    def test_unknown_mode_is_rejected_before_recipient_or_network(self):
        with patch.dict(os.environ, self.environment), patch.object(probe, 'public_get') as fetch:
            with self.assertRaisesRegex(ValueError, 'UNKNOWN_PROBE_MODE'):
                probe.run(argparse.Namespace(output='unused', mode='wallet-orders'))
            fetch.assert_not_called()

    def test_unverified_feed_clock_stops_before_any_market_request(self):
        from test_public_market_constraints import START, trade_record
        source = trade_record()
        source.update(request_started_ms=START + 59_000, received_ms=START + 60_000, finished_ms=START + 61_000)
        fetch = MagicMock()
        recorder = probe.HTTPRecorder(fetcher=fetch, clock=lambda: START + 20_000, mode='market-constraints')
        recorder.records.append(source)
        result = probe.observe_market_constraints(recorder)
        fetch.assert_not_called()
        self.assertEqual(result['selection']['status'], 'receipt_clock_order_unverified')
        self.assertEqual(result['interpretation'], 'receipt_clock_order_unverified')


if __name__ == '__main__':
    unittest.main()
