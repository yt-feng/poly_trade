"""Synthetic fixtures only; no source request, archives or network are used."""
import argparse
import base64
import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_source_coverage as runner
import persist_quality_first_journal as journal
import research_vault
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def fixture():
    request = {'version': 1, 'source_repository': 'yt-feng/poly', 'source_run_id': '12345',
               'source_run_attempt': 1, 'source_head_sha': 'a' * 40, 'release_id': 456,
               'release_tag': 'capture-v2-12345-1', 'max_bytes': runner.MAX_BYTES,
               'registered_utc': '2030-02-03T04:05:06Z', 'selection_rule': 'Synthetic name-ordered sample.',
               'audit_policy': {'decision_seconds_before_end': 60, 'anchor_max_age_ms': 2000,
                                'reference_max_age_ms': 4000, 'metadata_max_age_ms': 60000},
               'selected_assets': []}
    responses, assets = {}, []
    for index, kind in enumerate(('snapshots', 'labels')):
        name = f'{kind}-2030-02-03-00000{index + 1}.jsonl.gz'
        body = gzip.compress(json.dumps({'synthetic_kind': kind}).encode() + b'\n', mtime=0)
        sha = hashlib.sha256(body).hexdigest()
        side = (sha + '  ' + name + '\n').encode()
        asset = {'name': name, 'asset_id': 100 + index * 2, 'bytes': len(body),
                 'api_digest': 'sha256:' + sha, 'created_at': '2030-02-03T04:00:00Z',
                 'sidecar_asset_id': 101 + index * 2, 'sidecar_bytes': len(side),
                 'sidecar_api_digest': 'sha256:' + hashlib.sha256(side).hexdigest()}
        request['selected_assets'].append(asset)
        url = runner.DOWNLOAD + request['release_tag'] + '/' + name
        responses[url] = body; responses[url + '.sha256'] = side
        assets.extend([{'id': asset['asset_id'], 'name': name, 'size': len(body),
                        'digest': asset['api_digest'], 'created_at': asset['created_at']},
                       {'id': asset['sidecar_asset_id'], 'name': name + '.sha256',
                        'size': len(side), 'digest': asset['sidecar_api_digest']}])
    run = {'id': 12345, 'run_attempt': 1, 'head_sha': request['source_head_sha'],
           'repository': {'full_name': 'yt-feng/poly'}, 'status': 'in_progress', 'conclusion': None}
    release = {'id': 456, 'tag_name': request['release_tag'], 'assets': assets}
    responses[runner.API + '/actions/runs/12345'] = json.dumps(run).encode()
    responses[runner.API + '/releases/456'] = json.dumps(release).encode()
    return request, responses


class Response(io.BytesIO):
    def __init__(self, body, status=200, length=True):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Length': str(len(body))} if length else {}


class CoverageRunnerTests(unittest.TestCase):
    def setUp(self):
        self.request, self.responses = fixture()
        self.identity = X25519PrivateKey.generate()
        self.recipient = research_vault.recipient_for(self.identity)
        self.env = {'GITHUB_ACTIONS': 'true', 'SOURCE_COVERAGE_REQUEST_B64': encoded(self.request),
                    'QUALITY_FIRST_RECIPIENT_JSON_B64': encoded(self.recipient),
                    'QUALITY_FIRST_RECIPIENT_ID': self.recipient['recipient_id']}

    def decode(self, request):
        return runner.decode_request(encoded(request))[0]

    def recover(self, output):
        parts = []
        for path in sorted(output.glob('part-*.vault')):
            part = json.loads(research_vault.unseal(path.read_bytes(), self.identity))
            parts.append(base64.b64decode(part['payload_base64']))
        payload = b''.join(parts)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), part['bundle_sha256'])
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}

    def run_fixture(self, responses=None, env=None, audit=None):
        responses = self.responses if responses is None else responses
        calls = []

        def opened(request, timeout):
            calls.append(request.full_url)
            return Response(responses[request.full_url])

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, env or self.env), \
             patch.object(runner, 'build_opener') as build:
            build.return_value.open.side_effect = opened
            output = Path(temporary) / 'delivery'
            public = io.StringIO()
            with patch.object(runner.audit_module(), 'audit_records', audit or Mock(return_value={'synthetic_audit': True})) as fn, \
                 contextlib.redirect_stdout(public), contextlib.redirect_stderr(public):
                code = runner.run(argparse.Namespace(output=output))
            status = journal.inspect_delivery(output, self.recipient['recipient_id'])['status']
            self.assertEqual(set(p.name for p in output.iterdir()),
                             {'status.json'} | {f'part-{i:04d}.vault' for i in range(status['sealed_parts'])})
            return code, status, self.recover(output), public.getvalue(), calls, fn

    def test_request_and_plan_preserve_registered_order_without_catalog_selection(self):
        request = self.decode(self.request)
        plan = runner.pinned_plan(request)
        self.assertEqual([item['name'] for item in plan['items']], [item['name'] for item in request['selected_assets']])
        self.assertTrue(all(item['selected'] for item in plan['items']))
        self.assertIn('registered_assets_only', plan['selection_scope'])
        self.assertEqual(plan['max_releases'], 1)
        self.assertTrue(all(item['url'].startswith(runner.DOWNLOAD + request['release_tag'] + '/') for item in plan['items']))

    def test_source_identity_url_and_unknown_input_rejected_before_network(self):
        mutations = [('source_repository', 'someone/else'), ('source_run_id', '../12345'),
                     ('source_run_attempt', True), ('source_head_sha', 'not-a-commit'),
                     ('release_tag', 'capture-v2-12345-2'), ('release_id', False),
                     ('max_bytes', True), ('url', 'https://unregistered.invalid'),
                     ('registered_utc', '2030-02-03')]
        for key, value in mutations:
            with self.subTest(key=key), patch.object(runner, 'build_opener') as network:
                request = copy.deepcopy(self.request); request[key] = value
                with self.assertRaises((ValueError, TypeError)):
                    self.decode(request)
                network.assert_not_called()

    def test_asset_names_counts_ids_sizes_digests_and_policy_fail_closed(self):
        for key, value in [('name', '../snapshots-2030-02-03-000001.jsonl.gz'),
                           ('name', 'snapshots-2030-02-31-000001.jsonl.gz'),
                           ('asset_id', True), ('sidecar_asset_id', 100), ('bytes', False),
                           ('sidecar_bytes', 4097), ('api_digest', 'sha256:' + 'x' * 64),
                           ('sidecar_api_digest', None)]:
            with self.subTest(key=key, value=value):
                request = copy.deepcopy(self.request); request['selected_assets'][0][key] = value
                with self.assertRaises((ValueError, TypeError)):
                    self.decode(request)
        request = copy.deepcopy(self.request); request['selected_assets'] *= 2
        with self.assertRaises(ValueError):
            self.decode(request)
        request = copy.deepcopy(self.request); request['audit_policy']['anchor_max_age_ms'] = True
        with self.assertRaises(ValueError):
            self.decode(request)

    def test_strict_utf8_json_rejects_duplicates_nan_infinity_and_overflow(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\xff"}'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeDecodeError)):
                runner.strict_json(raw)

    def test_cloud_and_recipient_gates_precede_requests(self):
        for change in ({'GITHUB_ACTIONS': 'false'}, {'QUALITY_FIRST_RECIPIENT_JSON_B64': ''},
                       {'QUALITY_FIRST_RECIPIENT_ID': '0' * 64}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary, \
                 patch.dict(os.environ, {**self.env, **change}), patch.object(runner, 'build_opener') as network:
                with self.assertRaises(Exception):
                    runner.run(argparse.Namespace(output=Path(temporary) / 'delivery'))
                network.assert_not_called()

    def test_invalid_private_request_seals_failure_without_any_network(self):
        code, status, members, public, calls, audit = self.run_fixture(env={**self.env, 'SOURCE_COVERAGE_REQUEST_B64': 'invalid-secret'})
        self.assertEqual(code, 2); self.assertEqual(status['failed_stage'], 'acquisition')
        self.assertFalse(calls); audit.assert_not_called()
        self.assertNotIn('invalid-secret', public)
        self.assertIn('diagnostics.txt', members)

    def test_full_pipeline_preserves_raw_rows_and_policy_and_excludes_archives(self):
        code, status, members, public, calls, audit = self.run_fixture()
        self.assertEqual(code, 0); self.assertFalse(status['strategy_evaluated'])
        self.assertEqual(len(calls), 6)
        audit.assert_called_once()
        snapshot_rows, label_rows, policy = audit.call_args.args
        self.assertEqual(policy, self.request['audit_policy'])
        for rows, kind, asset in zip((snapshot_rows, label_rows), ('snapshots', 'labels'), self.request['selected_assets']):
            self.assertEqual(rows, [{'synthetic_kind': kind, '__source__': {
                'name': asset['name'], 'row_index': 1, 'sha256': asset['api_digest'][7:]}}])
        self.assertFalse(any('captured/' in name or name.endswith('.jsonl.gz') for name in members))
        self.assertEqual(json.loads(members['results/source-request.json']), self.request)
        self.assertIn('acquisition/acquisition_plan.json', members)
        self.assertIn('acquisition/acquisition_manifest.json', members)
        self.assertNotIn('synthetic_kind', public)
        verified = json.loads(members['results/source-verification.json'])
        self.assertEqual(verified['run']['status'], 'in_progress')
        self.assertTrue(verified['registered_identity_verified'])
        self.assertEqual(verified['http_body_bytes_read'], sum(len(self.responses[url]) for url in calls))

    def test_source_revision_mismatch_seals_without_downloading_or_auditing(self):
        responses = dict(self.responses)
        url = runner.API + '/actions/runs/12345'
        source = json.loads(responses[url]); source['head_sha'] = 'b' * 40
        source['private_fixture'] = 'PRIVATE_SOURCE_MARKER'
        responses[url] = json.dumps(source).encode()
        code, status, members, public, calls, audit = self.run_fixture(responses=responses)
        self.assertEqual(code, 2); self.assertEqual(calls, [url]); audit.assert_not_called()
        self.assertNotIn('PRIVATE_SOURCE_MARKER', public)
        self.assertIn(b'PRIVATE_SOURCE_MARKER', members['results/source-verification.json'])

    def test_registered_asset_metadata_and_sidecar_bytes_are_independently_verified(self):
        for mode in ('metadata', 'sidecar'):
            with self.subTest(mode=mode):
                responses = dict(self.responses)
                if mode == 'metadata':
                    url = runner.API + '/releases/456'
                    data = json.loads(responses[url]); data['assets'][0]['digest'] = 'sha256:' + 'c' * 64
                    responses[url] = json.dumps(data).encode()
                else:
                    url = next(key for key in responses if key.endswith('.sha256'))
                    responses[url] = b'0' + responses[url][1:]
                code, status, members, public, calls, audit = self.run_fixture(responses=responses)
                self.assertEqual(code, 2); audit.assert_not_called()
                self.assertEqual(status['failed_stage'], 'acquisition')
                if mode == 'sidecar':
                    manifest = json.loads(members['acquisition/acquisition_manifest.json'])
                    self.assertTrue(manifest['errors'])
                    self.assertEqual(manifest['eligible_archives_total'], 2)

    def test_missing_embedded_asset_uses_bounded_fixed_release_pages(self):
        responses = dict(self.responses); release_url = runner.API + '/releases/456'
        release = json.loads(responses[release_url]); assets = release['assets']; release['assets'] = []
        responses[release_url] = json.dumps(release).encode()
        page1 = assets[:3] + [{'id': 10_000 + index} for index in range(97)]
        base = runner.API + '/releases/456/assets?per_page=100&page='
        responses[base + '1'] = json.dumps(page1).encode(); responses[base + '2'] = json.dumps(assets[3:]).encode()
        code, _, members, _, calls, _ = self.run_fixture(responses=responses)
        self.assertEqual(code, 0); self.assertIn(base + '2', calls)
        self.assertEqual(len(json.loads(members['results/source-verification.json'])['asset_pages']), 2)

    def test_missing_registered_asset_never_substitutes_another(self):
        responses = dict(self.responses); url = runner.API + '/releases/456'
        release = json.loads(responses[url]); release['assets'] = release['assets'][1:]
        responses[url] = json.dumps(release).encode()
        responses[runner.API + '/releases/456/assets?per_page=100&page=1'] = json.dumps(release['assets']).encode()
        code, _, _, _, calls, audit = self.run_fixture(responses=responses)
        self.assertEqual(code, 2); audit.assert_not_called()
        self.assertFalse(any('/download/' in url for url in calls))

    def test_audit_failure_remains_private_and_restores_acquisition_functions(self):
        original_plan, original_request = runner.acquisition._plan, runner.acquisition.request
        code, status, members, public, _, _ = self.run_fixture(audit=Mock(side_effect=ValueError('PRIVATE_ANALYSIS_MARKER')))
        self.assertEqual(code, 2); self.assertEqual(status['failed_stage'], 'research')
        self.assertIn(b'PRIVATE_ANALYSIS_MARKER', members['diagnostics.txt']); self.assertNotIn('PRIVATE_ANALYSIS_MARKER', public)
        self.assertIs(original_plan, runner.acquisition._plan); self.assertIs(original_request, runner.acquisition.request)

    def test_transport_routes_cloud_gate_and_default_environment_handlers(self):
        transport = runner.PinnedTransport(self.request)
        url = runner.API + '/actions/runs/12345'
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(runner, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'GITHUB_ACTIONS_REQUIRED'):
                transport(url, json_response=True)
            opener.assert_not_called()
        with patch.dict(os.environ, self.env), patch.object(runner, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'UNREGISTERED'):
                transport('https://example.invalid')
            opener.assert_not_called()
            opener.return_value.open.return_value = Response(b'{}')
            self.assertEqual(transport(url, json_response=True), {})
            handlers = opener.call_args.args
            self.assertEqual(len(handlers), 1); self.assertIsInstance(handlers[0], runner.SafeRedirect)

    def test_transport_cumulative_response_budget_and_timeout_no_receipt(self):
        url = runner.API + '/actions/runs/12345'
        with patch.dict(os.environ, self.env), patch.object(runner, 'build_opener') as opener, \
             patch.object(runner, 'MAX_BYTES', 12), patch.object(runner.time, 'sleep'):
            transport = runner.PinnedTransport(self.request)
            opener.return_value.open.side_effect = [Response(b'{"x":1}'), Response(b'{"x":1}')]
            transport(url, json_response=True)
            with self.assertRaisesRegex(ValueError, 'HTTP_TOTAL_BUDGET'):
                transport(url, json_response=True)
            self.assertEqual(transport.bytes_read, 12)
        with patch.dict(os.environ, self.env), patch.object(runner, 'build_opener') as opener, patch.object(runner.time, 'sleep'):
            transport = runner.PinnedTransport(self.request)
            opener.return_value.open.side_effect = TimeoutError('PRIVATE_TIMEOUT')
            with self.assertRaises(runner.acquisition.TransportError):
                transport(url, json_response=True)
            self.assertEqual(len(transport.records), 2)
            self.assertTrue(all(row['received_ms'] is None for row in transport.records))
            self.assertNotIn('PRIVATE_TIMEOUT', json.dumps(transport.records))

    def test_transport_body_size_and_digest_mismatch_fail_before_parse(self):
        url = next(url for url in self.responses if url.endswith('.jsonl.gz'))
        with patch.dict(os.environ, self.env), patch.object(runner, 'build_opener') as opener:
            transport = runner.PinnedTransport(self.request)
            opener.return_value.open.return_value = Response(self.responses[url] + b'x')
            with self.assertRaisesRegex(ValueError, 'HTTP_RESPONSE_SIZE'):
                transport(url)
            self.assertEqual(transport.bytes_read, 0)
            opener.return_value.open.return_value = Response(b'x' * len(self.responses[url]))
            with self.assertRaisesRegex(ValueError, 'REGISTERED_BODY_MISMATCH'):
                transport(url)

    def test_error_status_precedes_small_sidecar_body_limit(self):
        url = next(url for url in self.responses if url.endswith('.sha256'))
        for status in (403, 451, 503):
            with self.subTest(status=status), patch.dict(os.environ, self.env), \
                 patch.object(runner, 'build_opener') as opener, patch.object(runner.time, 'sleep'):
                transport = runner.PinnedTransport(self.request)
                opener.return_value.open.side_effect = lambda *a, **k: Response(b'x' * 10000, status=status)
                expected = runner.acquisition.AccessRefused if status in (403, 451) else runner.acquisition.TransportError
                with self.assertRaises(expected) as caught:
                    transport(url)
                self.assertEqual(caught.exception.code, status)
                self.assertEqual(opener.return_value.open.call_count, 1 if status in (403, 451) else 2)
                self.assertEqual(transport.bytes_read, 0)
                self.assertTrue(all(r['http_status'] == status for r in transport.records))

    def decoded(self, lines, **limits):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'snapshots-2030-02-03-000001.jsonl.gz'
            body = gzip.compress(lines, mtime=0); path.write_bytes(body)
            manifest = {'files': [{'path': path.name, 'name': path.name, 'bytes': len(body),
                                   'sha256': hashlib.sha256(body).hexdigest()}]}
            with contextlib.ExitStack() as stack:
                for name, value in limits.items():
                    stack.enter_context(patch.object(runner, name, value))
                stats = {}; result = runner.load_records(root, manifest, stats)
            return result, stats

    def test_streamed_decode_retains_records_and_enforces_line_rows_and_total_limits(self):
        result, stats = self.decoded(b'{"x":1}\n{"x":2}\n')
        self.assertEqual([row['x'] for row in result[0]], [1, 2])
        self.assertEqual([row['__source__']['row_index'] for row in result[0]], [1, 2])
        self.assertEqual(result[1], [])
        self.assertEqual(stats['rows'], 2); self.assertTrue(stats['files'][0]['complete'])
        for limits, reason in [({'MAX_LINE_BYTES': 4}, 'INFLATED_LINE_LIMIT'),
                               ({'MAX_ROWS': 1}, 'ROW_LIMIT'),
                               ({'MAX_INFLATED_BYTES': 8}, 'INFLATED_TOTAL_LIMIT')]:
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, reason):
                self.decoded(b'{"x":1}\n{"x":2}\n', **limits)
        for data in (b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'{"x":1e999}\n', b'[]\n', b'\xff\n', b'\n', b'{"__source__":{}}\n'):
            with self.subTest(data=data), self.assertRaises((ValueError, UnicodeDecodeError)):
                self.decoded(data)


if __name__ == '__main__':
    unittest.main()
