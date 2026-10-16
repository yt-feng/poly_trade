"""Fake Git Data API tests: no GitHub calls, credentials or real research."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
                raise publisher.ApiError()
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
                raise publisher.ApiError()
            if self.ref not in self.commits[payload['sha']]['parents']:
                raise publisher.ApiError()
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

    def publish(self):
        return publisher.publish(self.source, publisher.REPOSITORY, '123', '1', self.rid, api=self.api)

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


if __name__ == '__main__':
    unittest.main()
