"""Fake Git Data API tests: no GitHub calls, credentials or real research."""
import base64
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import persist_quality_first_journal as journal
import publish_quality_first_journal as publisher
import research_vault
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


class FakeGitDataAPI:
    def __init__(self):
        self.ref = None
        self.blobs = {}
        self.trees = {}
        self.commits = {}
        self.calls = []
        self.uncertain_patch_once = False
        self.concurrent_patch_once = False
        self.concurrent_path = 'research_vault/episodes/' + 'b' * 64 + '/part-0000.vault'

    def save(self, table, value):
        sha = hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()
        table[sha] = copy.deepcopy(value)
        return sha

    def seed_branch(self, files=None):
        tree = self.save(self.trees, files or {})
        self.ref = self.save(self.commits, {'tree': {'sha': tree}, 'parents': []})

    def current_files(self):
        return copy.deepcopy(self.trees[self.commits[self.ref]['tree']['sha']]) if self.ref else {}

    def count(self, method, suffix):
        return sum(m == method and p.endswith(suffix) for m, p, _ in self.calls)

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        prefix = 'repos/' + publisher.REPOSITORY + '/git/'
        if not path.startswith(prefix):
            raise AssertionError('Unexpected API path')
        suffix = path[len(prefix):]
        if method == 'GET' and suffix == 'ref/heads/' + publisher.BRANCH:
            if self.ref is None:
                raise publisher.ApiError(not_found=True)
            return {'object': {'sha': self.ref}}
        if method == 'GET' and suffix.startswith('commits/'):
            return copy.deepcopy(self.commits[suffix[8:]])
        if method == 'GET' and suffix.startswith('trees/'):
            tree = self.trees[suffix[6:].split('?', 1)[0]]
            return {'truncated': False, 'tree': [dict(value, path=name) for name, value in tree.items()]}
        if method == 'POST' and suffix == 'blobs':
            data = base64.b64decode(payload['content'], validate=True)
            sha = publisher.blob_id(data)
            self.blobs[sha] = data
            return {'sha': sha}
        if method == 'POST' and suffix == 'trees':
            tree = copy.deepcopy(self.trees.get(payload.get('base_tree'), {}))
            tree.update({row['path']: {key: row[key] for key in ('sha', 'mode', 'type')} for row in payload['tree']})
            return {'sha': self.save(self.trees, tree)}
        if method == 'POST' and suffix == 'commits':
            value = {'tree': {'sha': payload['tree']}, 'parents': payload['parents']}
            return {'sha': self.save(self.commits, value)}
        if method == 'POST' and suffix == 'refs':
            if self.ref is not None:
                raise publisher.ApiError(status_code=422, category='ref_already_exists')
            if payload['ref'] != 'refs/heads/' + publisher.BRANCH:
                raise AssertionError('Wrong branch')
            self.ref = payload['sha']
            return {'object': {'sha': self.ref}}
        if method == 'PATCH' and suffix == 'refs/heads/' + publisher.BRANCH:
            if payload.get('force') is not False:
                raise AssertionError('Force update forbidden')
            if self.concurrent_patch_once:
                self.concurrent_patch_once = False
                tree = self.current_files()
                tree[self.concurrent_path] = {'sha': 'c' * 40, 'mode': '100644', 'type': 'blob'}
                tree_sha = self.save(self.trees, tree)
                self.ref = self.save(self.commits, {'tree': {'sha': tree_sha}, 'parents': [self.ref]})
                raise publisher.ApiError(status_code=422, category='ref_non_fast_forward')
            if self.ref not in self.commits[payload['sha']]['parents']:
                raise publisher.ApiError(status_code=422, category='ref_non_fast_forward')
            self.ref = payload['sha']
            if self.uncertain_patch_once:
                self.uncertain_patch_once = False
                raise subprocess.TimeoutExpired('synthetic API call', 90)
            return {'object': {'sha': self.ref}}
        raise AssertionError('Unexpected API operation')


class JournalPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / 'delivery'
        self.source.mkdir()
        private = X25519PrivateKey.generate()
        recipient = research_vault.recipient_for(private)
        self.recipient = recipient
        self.rid = recipient['recipient_id']
        (self.source / 'part-0000.vault').write_bytes(research_vault.seal(b'synthetic research only', recipient, self.rid))
        (self.source / 'status.json').write_text(json.dumps({
            'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
            'strategy_evaluated': True, 'live_orders_enabled': False,
            'plaintext_published': False, 'sealed_parts': 1}))
        self.api = FakeGitDataAPI()
        episode = journal.episode_identity(publisher.REPOSITORY, '123', '1')
        self.prefix = 'research_vault/episodes/' + episode + '/'

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, api=None):
        return publisher.publish(self.source, publisher.REPOSITORY, '123', '1', self.rid, api=api or self.api)

    def ref_fault(self, error, *, applied=False, advance=False, count=None, read_error=None):
        """Inject only synthetic ref outcomes; all objects stay in FakeGitDataAPI."""
        api = self.api
        writes = 0
        def injected(method, path, payload=None):
            nonlocal writes
            if method in {'PATCH', 'POST'} and '/git/refs' in path:
                writes += 1
                if count is not None and writes > count:
                    return api(method, path, payload)
                if applied:
                    api(method, path, payload)
                else:
                    api.calls.append((method, path, copy.deepcopy(payload)))
                if advance:
                    tree = api.current_files()
                    tree['other-' + str(writes)] = {'sha': 'a' * 40, 'mode': '100644', 'type': 'blob'}
                    tree_sha = api.save(api.trees, tree)
                    api.ref = api.save(api.commits, {'tree': {'sha': tree_sha}, 'parents': [api.ref] if api.ref else []})
                if error is not None:
                    raise error(method) if callable(error) else error
                return {}
            if writes and read_error is not None:
                api.calls.append((method, path, copy.deepcopy(payload)))
                raise read_error
            return api(method, path, payload)
        return injected

    def prepare_ref(self, method):
        self.api = FakeGitDataAPI()
        if method == 'PATCH':
            self.api.seed_branch({'unrelated': {'sha': 'a' * 40, 'mode': '100644', 'type': 'blob'}})

    def ref_writes(self):
        return [(m, p, body) for m, p, body in self.api.calls if m in {'POST', 'PATCH'} and '/git/refs' in p]

    def test_first_publication_creates_orphan_journal_branch_and_reads_back(self):
        result = self.publish()
        self.assertEqual(result['status'], 'verified_remote')
        self.assertEqual(self.api.count('POST', '/commits'), 1)
        commits = [body for method, path, body in self.api.calls if method == 'POST' and path.endswith('/commits')]
        self.assertEqual(commits[0]['parents'], [])
        self.assertEqual(self.api.count('POST', '/refs'), 1)
        self.assertEqual(set(self.api.current_files()), {self.prefix + 'part-0000.vault', self.prefix + 'status.json'})
        envelope = self.api.current_files()[self.prefix + 'part-0000.vault']
        self.assertEqual(self.api.blobs[envelope['sha']], (self.source / 'part-0000.vault').read_bytes())
        self.assertFalse(any('heads/main' in path for _, path, _ in self.api.calls))

    def test_uncertain_patch_success_is_read_back_without_duplicate_write(self):
        self.api.seed_branch()
        self.api.uncertain_patch_once = True
        self.assertEqual(self.publish()['status'], 'verified_remote')
        self.assertEqual(self.api.count('PATCH', '/refs/heads/' + publisher.BRANCH), 1)
        self.assertEqual(self.api.count('POST', '/commits'), 1)
        self.assertGreaterEqual(self.api.count('GET', '/ref/heads/' + publisher.BRANCH), 2)

    def test_existing_episode_is_idempotent_and_preserves_other_entries(self):
        unrelated = {'unrelated.vault': {'sha': 'a' * 40, 'mode': '100644', 'type': 'blob'}}
        self.api.seed_branch(unrelated)
        self.publish()
        before = copy.deepcopy(self.api.current_files())
        before_calls = len(self.api.calls)
        self.assertEqual(self.publish()['status'], 'verified_remote')
        self.assertEqual(self.api.current_files(), before)
        self.assertEqual(self.api.current_files()['unrelated.vault'], unrelated['unrelated.vault'])
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before_calls:]))

    def test_conflicting_existing_episode_refuses_all_writes(self):
        self.api.seed_branch({self.prefix + 'part-0000.vault': {'sha': 'a' * 40, 'mode': '100644', 'type': 'blob'}})
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            self.publish()
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_concurrent_ref_update_rebases_on_new_tree_without_losing_new_log(self):
        self.api.seed_branch()
        self.api.concurrent_patch_once = True
        self.assertEqual(self.publish()['status'], 'verified_remote')
        actual = self.api.current_files()
        self.assertIn(self.api.concurrent_path, actual)
        self.assertIn(self.prefix + 'part-0000.vault', actual)
        self.assertEqual(self.api.count('PATCH', '/refs/heads/' + publisher.BRANCH), 2)
        self.assertEqual(self.api.count('POST', '/blobs'), 2)
        trees = [payload for method, path, payload in self.api.calls if method == 'POST' and path.endswith('/trees')]
        self.assertNotEqual(trees[0]['base_tree'], trees[1]['base_tree'])

    def test_remote_mode_or_extra_tree_is_not_false_byte_verification(self):
        self.publish()
        original = self.api.current_files()
        for corruption in ['symlink', 'executable', 'extra_tree', 'extra_gitlink']:
            tree = copy.deepcopy(original)
            if corruption in ['symlink', 'executable']:
                tree[self.prefix + 'part-0000.vault']['mode'] = '120000' if corruption == 'symlink' else '100755'
            else:
                tree[self.prefix + 'extra'] = {'sha': 'a' * 40,
                    'mode': '040000' if corruption == 'extra_tree' else '160000',
                    'type': 'tree' if corruption == 'extra_tree' else 'commit'}
            self.api.seed_branch(tree)
            before = len(self.api.calls)
            with self.assertRaisesRegex(ValueError, 'CONFLICT'):
                self.publish()
            self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))

    def test_unscoped_distinct_matrix_deliveries_still_conflict_without_overwrite(self):
        self.publish()
        original = self.api.current_files()
        before = len(self.api.calls)
        (self.source / 'part-0000.vault').write_bytes(research_vault.seal(
            b'synthetic different matrix cell', self.recipient, self.rid))
        with self.assertRaisesRegex(ValueError, 'EXISTING_EPISODE_CONFLICT'):
            self.publish()
        self.assertEqual(self.api.current_files(), original)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))

    def test_opt_in_cells_coexist_with_legacy_retry_and_reject_changed_same_cell(self):
        legacy = self.publish()
        identities = {legacy['episode_id']}
        for label in ('h373-crossing-0.0', 'h373-crossing-0.25'):
            previous = self.api.current_files()
            (self.source / 'part-0000.vault').write_bytes(research_vault.seal(
                ('synthetic ' + label).encode(), self.recipient, self.rid))
            result = publisher.publish(self.source, publisher.REPOSITORY, '123', '1',
                                       self.rid, self.api, cell_id=label)
            identities.add(result['episode_id'])
            self.assertEqual(result['status'], 'verified_remote')
            actual = self.api.current_files()
            self.assertTrue(all(actual[path] == value for path, value in previous.items()))
            self.assertFalse(any(label in path for path in actual))
            before = len(self.api.calls)
            retry = publisher.publish(self.source, publisher.REPOSITORY, '123', '1',
                                      self.rid, api=self.api, cell_id=label)
            self.assertEqual(retry['episode_id'], result['episode_id'])
            self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))
        self.assertEqual(len(identities), 3)
        self.assertEqual(len(self.api.current_files()), 6)
        previous = self.api.current_files()
        before = len(self.api.calls)
        (self.source / 'part-0000.vault').write_bytes(research_vault.seal(
            b'changed same cell', self.recipient, self.rid))
        with self.assertRaisesRegex(ValueError, 'EXISTING_EPISODE_CONFLICT'):
            publisher.publish(self.source, publisher.REPOSITORY, '123', '1',
                              self.rid, api=self.api, cell_id='h373-crossing-0.25')
        self.assertEqual(self.api.current_files(), previous)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))

    def test_cell_identity_survives_concurrent_ref_retry(self):
        self.publish()
        self.api.concurrent_patch_once = True
        with patch.object(publisher.time, 'sleep'):
            result = publisher.publish(self.source, publisher.REPOSITORY, '123', '1',
                                       self.rid, api=self.api, cell_id='h373-crossing-0.25')
        actual = self.api.current_files()
        self.assertIn(self.api.concurrent_path, actual)
        self.assertIn(self.prefix + 'part-0000.vault', actual)
        self.assertIn('research_vault/episodes/' + result['episode_id'] + '/part-0000.vault', actual)
        self.assertEqual(result['status'], 'verified_remote')

    def test_invalid_cell_is_rejected_before_api_access(self):
        for label in ('', '../escape', 'a' * 65, True):
            with self.subTest(cell=label), self.assertRaisesRegex(ValueError, 'INVALID_CELL_IDENTITY'):
                publisher.publish(self.source, publisher.REPOSITORY, '123', '1',
                                  self.rid, api=self.api, cell_id=label)
        self.assertEqual(self.api.calls, [])

    def test_ref_rejections_stop_after_one_write_without_readback_or_sleep(self):
        for method in ('PATCH', 'POST'):
            for status, category in ((401, 'authentication'), (403, 'permission'), (400, 'validation'),
                                     (404, 'not_found'), (429, 'rate_limit'), (409, 'conflict'),
                                     (422, 'validation'), (409, 'unknown'), (422, 'unknown'),
                                     (None, 'unknown'), (422, 'ref_already_exists' if method == 'PATCH' else 'ref_non_fast_forward')):
                with self.subTest(method=method, category=category), patch.object(publisher.time, 'sleep') as sleep:
                    self.prepare_ref(method)
                    error = publisher.ApiError(status_code=status, category=category)
                    with self.assertRaises(publisher.ApiError) as raised:
                        self.publish(self.ref_fault(error))
                    self.assertIs(raised.exception, error)
                    self.assertEqual(len(self.ref_writes()), 1)
                    self.assertEqual(self.api.calls[-1], self.ref_writes()[0])
                    self.assertEqual(self.api.count('POST', '/commits'), 1)
                    sleep.assert_not_called()

    def test_ref_races_require_new_parent_and_have_six_write_limit(self):
        def race(method):
            return publisher.ApiError(status_code=422, category=(
                'ref_non_fast_forward' if method == 'PATCH' else 'ref_already_exists'))
        for method in ('PATCH', 'POST'):
            for advance, count in ((False, None), (True, 1), (True, 5), (True, None)):
                with self.subTest(method=method, advance=advance, count=count), patch.object(publisher.time, 'sleep') as sleep:
                    self.prepare_ref(method)
                    injected = self.ref_fault(race, advance=advance, count=count)
                    if count is not None:
                        self.assertEqual(self.publish(injected)['status'], 'verified_remote')
                        self.assertIn('other-1', self.api.current_files())
                        if method == 'PATCH':
                            self.assertIn('unrelated', self.api.current_files())
                        expected = count + 1
                    else:
                        with self.assertRaises(publisher.ApiError) as raised:
                            self.publish(injected)
                        self.assertEqual(raised.exception.status_code, 422)
                        self.assertEqual(raised.exception.category, race('PATCH' if advance else method).category)
                        expected = 6 if advance else 1
                    self.assertEqual(len(self.ref_writes()), expected)
                    self.assertEqual(self.api.count('POST', '/commits'), expected)
                    self.assertEqual(self.api.count('POST', '/blobs'), 2)
                    self.assertEqual(sleep.call_count, expected - 1)
                    for verb, _, body in self.ref_writes():
                        if verb == 'PATCH':
                            self.assertIs(body['force'], False)

    def test_uncertain_or_successful_ref_writes_never_replay_without_confirmation(self):
        errors = [subprocess.TimeoutExpired('SYNTHETIC_PRIVATE_COMMAND', 90),
                  publisher.ApiError(category='invalid_response'),
                  publisher.ApiError(status_code=503, category='server_error'), None]
        for method in ('PATCH', 'POST'):
            for error in errors:
                for applied in (False, True):
                    with self.subTest(method=method, error=type(error).__name__, applied=applied), patch.object(publisher.time, 'sleep') as sleep:
                        self.prepare_ref(method)
                        injected = self.ref_fault(error, applied=applied, advance=True)
                        if applied:
                            self.assertEqual(self.publish(injected)['status'], 'verified_remote')
                        else:
                            with self.assertRaises(publisher.ApiError) as raised:
                                self.publish(injected)
                            expected = ('unverified_write' if error is None else
                                        'timeout' if isinstance(error, subprocess.TimeoutExpired) else error.category)
                            self.assertEqual(raised.exception.category, expected)
                        self.assertEqual(len(self.ref_writes()), 1)
                        self.assertEqual(self.api.count('POST', '/commits'), 1)
                        sleep.assert_not_called()

    def test_uncertain_ref_readback_failure_preserves_original_cause(self):
        for method in ('PATCH', 'POST'):
            for read_error in (publisher.ApiError(status_code=403, category='permission'),
                               subprocess.TimeoutExpired('SYNTHETIC_READ_COMMAND', 90),
                               publisher.ApiError(category='invalid_response'),
                               ValueError('JOURNAL_TREE_TRUNCATED')):
                with self.subTest(method=method, read_error=type(read_error).__name__), patch.object(publisher.time, 'sleep') as sleep:
                    self.prepare_ref(method)
                    error = subprocess.TimeoutExpired('SYNTHETIC_WRITE_COMMAND', 90)
                    with self.assertRaises(publisher.ApiError) as raised:
                        self.publish(self.ref_fault(error, applied=True, read_error=read_error))
                    self.assertEqual(raised.exception.category, 'timeout')
                    self.assertEqual(len(self.ref_writes()), 1)
                    sleep.assert_not_called()

    def test_ref_race_with_matching_episode_succeeds_without_retry(self):
        for method in ('PATCH', 'POST'):
            with self.subTest(method=method), patch.object(publisher.time, 'sleep') as sleep:
                self.prepare_ref(method)
                error = publisher.ApiError(status_code=422, category=(
                    'ref_non_fast_forward' if method == 'PATCH' else 'ref_already_exists'))
                self.assertEqual(self.publish(self.ref_fault(error, applied=True))['status'], 'verified_remote')
                self.assertEqual(len(self.ref_writes()), 1)
                sleep.assert_not_called()

    def test_successful_ref_ack_tolerates_one_eventually_consistent_read(self):
        self.prepare_ref('PATCH')
        original = self.api
        stale_ref = self.api.ref
        stale_reads = 1
        wrote = False

        def eventually_consistent(method, path, payload=None):
            nonlocal stale_reads, wrote
            result = original(method, path, payload)
            if method in {'PATCH', 'POST'} and '/git/refs' in path:
                wrote = True
            if wrote and method == 'GET' and path.endswith('/git/ref/heads/' + publisher.BRANCH) and stale_reads:
                stale_reads -= 1
                return {'object': {'sha': stale_ref}}
            return result

        with patch.object(publisher.time, 'sleep') as sleep:
            self.assertEqual(self.publish(eventually_consistent)['status'], 'verified_remote')
        self.assertEqual(stale_reads, 0)
        self.assertEqual(len(self.ref_writes()), 1)
        self.assertEqual(self.api.count('POST', '/commits'), 1)
        sleep.assert_called_once_with(0.5)

    def test_ref_readback_conflict_fails_without_retry(self):
        self.api.seed_branch()
        original = self.api
        def api(method, path, payload=None):
            result = original(method, path, payload)
            if method == 'PATCH':
                tree = original.current_files()
                tree[self.prefix + 'part-0000.vault']['mode'] = '100755'
                original.seed_branch(tree)
                raise subprocess.TimeoutExpired('synthetic', 90)
            return result
        with patch.object(publisher.time, 'sleep') as sleep, self.assertRaisesRegex(ValueError, 'CONFLICT'):
            self.publish(api)
        self.assertEqual(len(self.ref_writes()), 1)
        sleep.assert_not_called()

    def test_malformed_readback_stops_with_original_uncertain_cause(self):
        for suffix in ('/git/ref/', '/git/commits/', '/git/trees/'):
            with self.subTest(suffix=suffix), patch.object(publisher.time, 'sleep') as sleep:
                self.prepare_ref('PATCH')
                original = self.api
                wrote = False
                def api(method, path, payload=None):
                    nonlocal wrote
                    if wrote and suffix in path:
                        original.calls.append((method, path, payload))
                        return {'SYNTHETIC_PRIVATE_KEY': 'SYNTHETIC_PRIVATE_PAYLOAD'}
                    result = original(method, path, payload)
                    if method == 'PATCH':
                        wrote = True
                        raise publisher.ApiError(category='invalid_response')
                    return result
                with self.assertRaises(publisher.ApiError) as raised:
                    self.publish(api)
                self.assertEqual(raised.exception.category, 'invalid_response')
                self.assertEqual(len(self.ref_writes()), 1)
                sleep.assert_not_called()

    def test_object_creation_errors_are_never_retried(self):
        for stage in ('blobs', 'trees', 'commits'):
            for error in (publisher.ApiError(status_code=403, category='permission'),
                          publisher.ApiError(category='invalid_response'),
                          subprocess.TimeoutExpired('SYNTHETIC_PRIVATE_COMMAND', 90)):
                with self.subTest(stage=stage, error=type(error).__name__), patch.object(publisher.time, 'sleep') as sleep:
                    self.prepare_ref('PATCH')
                    def api(method, path, payload=None):
                        if method == 'POST' and path.endswith('/' + stage):
                            self.api.calls.append((method, path, payload))
                            raise error
                        return self.api(method, path, payload)
                    with self.assertRaises(type(error)) as raised:
                        self.publish(api)
                    self.assertIs(raised.exception, error)
                    self.assertEqual(self.api.count('POST', '/' + stage), 1)
                    self.assertEqual(self.ref_writes(), [])
                    sleep.assert_not_called()

    def test_cli_optional_cell_uses_real_publisher_with_fake_api(self):
        original_publish = publisher.publish
        def offline_publish(*args, **kwargs):
            return original_publish(*args, api=self.api, **kwargs)
        identities = []
        for label in (None, 'h373-crossing-0.25', '../invalid'):
            argv = ['publish', '--source', str(self.source), '--repository', publisher.REPOSITORY,
                    '--run-id', '123', '--run-attempt', '1', '--recipient-id', self.rid]
            if label is not None:
                argv += ['--cell-id', label]
            output = io.StringIO()
            before = len(self.api.calls)
            with patch.object(sys, 'argv', argv), patch.object(publisher, 'publish', side_effect=offline_publish), redirect_stdout(output):
                code = publisher.main()
            result = json.loads(output.getvalue())
            if label == '../invalid':
                self.assertEqual(code, 2)
                self.assertEqual(result['status'], 'publication_failed')
                self.assertEqual(len(self.api.calls), before)
            else:
                self.assertEqual(code, 0)
                identities.append(result['episode_id'])
            if label:
                self.assertNotIn(label, output.getvalue())
        self.assertNotEqual(*identities)


class ApiResponseTests(unittest.TestCase):
    def invoke(self, method, suffix, status, body, payload):
        result = subprocess.CompletedProcess(['SYNTHETIC_PRIVATE_COMMAND'], 1 if status else 0,
            stdout=body, stderr=f'gh: SYNTHETIC_PRIVATE_DIAGNOSTIC (HTTP {status})' if status else '')
        with patch.object(publisher.subprocess, 'run', return_value=result):
            return publisher.github_api(method, 'repos/' + publisher.REPOSITORY + '/git/' + suffix, payload)

    def test_only_exact_operation_and_422_message_identify_ref_race(self):
        cases = [('PATCH', 'refs/heads/' + publisher.BRANCH, {'sha': 'a' * 40, 'force': False},
                  'Update is not a fast forward', 'ref_non_fast_forward'),
                 ('POST', 'refs', {'sha': 'a' * 40, 'ref': 'refs/heads/' + publisher.BRANCH},
                  'Reference already exists', 'ref_already_exists')]
        for method, suffix, payload, message, category in cases:
            for change in ('none', 'status', 'message', 'method', 'path', 'payload', 'force_or_ref', 'errors', 'non_json'):
                with self.subTest(method=method, change=change):
                    body = {'message': message, 'private': 'SYNTHETIC_PRIVATE_PAYLOAD'}
                    if change == 'message':
                        body['message'] += ' SYNTHETIC_PRIVATE_DIAGNOSTIC'
                    if change == 'errors':
                        body['errors'] = [{'code': 'invalid'}]
                    with self.assertRaises(publisher.ApiError) as raised:
                        self.invoke('POST' if method == 'PATCH' and change == 'method' else
                                    'PATCH' if change == 'method' else method,
                                    'blobs' if change == 'path' else suffix,
                                    409 if change == 'status' else 422,
                                    'not JSON' if change == 'non_json' else json.dumps(body),
                                    {} if change == 'payload' else
                                    dict(payload, force=True, ref='refs/heads/other') if change == 'force_or_ref' else payload)
                    self.assertEqual(raised.exception.category, category if change == 'none' else
                                     'conflict' if change == 'status' else 'validation')

    def test_rejections_and_server_errors_retain_safe_classification(self):
        for status, category in ((401, 'authentication'), (403, 'permission'), (404, 'not_found'),
                                 (400, 'validation'), (409, 'conflict'), (422, 'validation'),
                                 (429, 'rate_limit'), (408, 'timeout'), (503, 'server_error')):
            with self.subTest(status=status), self.assertRaises(publisher.ApiError) as raised:
                self.invoke('PATCH', 'refs/heads/' + publisher.BRANCH, status,
                            json.dumps({'message': 'SYNTHETIC_PRIVATE_PAYLOAD'}), {'force': False})
            self.assertEqual((raised.exception.status_code, raised.exception.category), (status, category))
        with patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['synthetic'], 1, stdout='{}', stderr='gh: API rate limit exceeded (HTTP 403)')):
            with self.assertRaises(publisher.ApiError) as raised:
                publisher.github_api('PATCH', 'synthetic', {})
        self.assertEqual((raised.exception.status_code, raised.exception.category), (403, 'rate_limit'))

    def test_successful_http_with_invalid_json_is_narrowly_classified(self):
        for body in ('SYNTHETIC_PRIVATE_NON_JSON', '[]', 'null', '"SYNTHETIC_PRIVATE_STRING"'):
            with self.subTest(body=body), self.assertRaises(publisher.ApiError) as raised:
                self.invoke('POST', 'refs', None, body, {})
            self.assertEqual(raised.exception.category, 'invalid_response')
        self.assertEqual(self.invoke('POST', 'blobs', None, '{"sha":"synthetic"}', {}), {'sha': 'synthetic'})

    def test_cli_error_telemetry_is_allowlisted_and_never_contains_diagnostics(self):
        sentinel = 'SYNTHETIC_PRIVATE_PATH_COMMAND_PAYLOAD'
        errors = [publisher.ApiError(status_code=403, category='permission'),
                  publisher.ApiError(status_code=sentinel, category=sentinel),
                  type(sentinel, (Exception,), {})(sentinel),
                  subprocess.TimeoutExpired(sentinel, 90, output=sentinel, stderr=sentinel),
                  ValueError(sentinel)]
        for error in errors:
            output = io.StringIO()
            stderr = io.StringIO()
            with self.subTest(error=type(error).__name__), patch.object(sys, 'argv',
                    ['publish', '--source', sentinel, '--run-id', '123', '--run-attempt', '1']), \
                    patch.object(publisher, 'publish', side_effect=error), redirect_stdout(output), redirect_stderr(stderr):
                self.assertEqual(publisher.main(), 2)
            self.assertEqual(stderr.getvalue(), '')
            self.assertNotIn(sentinel, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(set(result), {'status', 'plaintext_published', 'error_class', 'error_code', 'error_category'})
            self.assertEqual(result['status'], 'publication_failed')
            self.assertIs(result['plaintext_published'], False)
            self.assertIn(result['error_category'], publisher.ERROR_CATEGORIES)
            self.assertIn(result['error_class'], publisher.ERROR_CLASSES | {'Exception'})
            self.assertIn(result['error_code'], publisher.ERROR_CODES | {None})


if __name__ == '__main__':
    unittest.main()
