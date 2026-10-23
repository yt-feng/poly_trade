"""Synthetic two-envelope input transport; no historical table or network reads."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault
import scoped_research_dataset as dataset
import restore_quality_capacity_inputs as loader

REPOSITORY = 'synthetic/research'
CODE = 'a' * 40


def make_inputs(root):
    """Create two independently keyed tiny CSV deliveries using established crypto."""
    key = vault.X25519PrivateKey.generate()
    recipient = vault.recipient_for(key)
    raw = {
        'dataset': {'features.csv': b'market_id,feature\nexample,0.25\n',
            'quote-cash-domains.csv': b'market_id,quote_status\nexample,selected\n',
            'provenance.json': vault.canonical({'format': 'synthetic-dataset-provenance-v1'})},
        'control': {'control-predictions.csv': b'market_id,prediction\nexample,0.75\n',
            'provenance.json': vault.canonical({'format': 'synthetic-control-provenance-v1'})},
    }
    capabilities, descriptors, envelopes = {}, {}, {}
    config = {'expected_code_commit': CODE, 'private_inputs': {}, 'cache_bindings': {}}
    for index, (kind, members) in enumerate(raw.items()):
        directory = root / ('source-' + kind)
        directory.mkdir()
        paths = {}
        for name, value in members.items():
            paths[name] = directory / name
            paths[name].write_bytes(value)
        envelope, cap, descriptor = dataset.pack(paths, recipient, recipient['recipient_id'])
        capabilities[kind], descriptors[kind], envelopes[kind] = cap, descriptor, envelope
        config['private_inputs'][kind] = {'descriptor_sha256': dataset.descriptor_sha256(descriptor),
            'source': {'repository': REPOSITORY, 'commit': ('b' if index == 0 else 'c')*40,
                'path': 'research_vault/episodes/' + ('d' if index == 0 else 'e')*64 + '/part-0000.vault'}}
        for name, binding in loader.INPUT_FILES[kind].items():
            config['cache_bindings'][binding] = hashlib.sha256(raw[kind][name]).hexdigest()
    return config, capabilities, descriptors, envelopes, raw


class RestoreQualityCapacityInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.caps, self.descriptors, self.envelopes, self.raw = make_inputs(self.root)
        self.bootstrap = self.root / 'bootstrap'
        self.bootstrap.mkdir(mode=0o700)

    def restore(self, config=None, caps=None, fetch=None, **kwargs):
        return loader.restore_inputs(config or self.config, caps or self.caps, self.bootstrap,
            repository=kwargs.get('repository', REPOSITORY), code_commit=kwargs.get('code_commit', CODE),
            fetch=fetch or self.fetch)

    def fetch(self, source, maximum, *, token=None):
        kind = next(kind for kind in self.config['private_inputs']
                    if source == self.config['private_inputs'][kind]['source'])
        self.assertEqual(maximum, len(self.envelopes[kind]))
        return self.envelopes[kind]

    def test_separate_capabilities_restore_exact_files_and_keyless_receipt(self):
        calls = []
        def fetch(source, maximum, *, token=None):
            calls.append((copy.deepcopy(source), maximum, token))
            return self.fetch(source, maximum, token=token)
        with mock.patch.object(vault, 'unlock_identity', side_effect=AssertionError('identity forbidden')):
            with mock.patch.dict(os.environ, {'GH_TOKEN': 'synthetic-access-token'}):
                receipt = self.restore(fetch=fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item[0] for item in calls], [self.config['private_inputs'][kind]['source'] for kind in loader.INPUT_FILES])
        self.assertTrue(all(item[2] == 'synthetic-access-token' for item in calls))
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(receipt['global_identity_unlocks'], 0)
        self.assertFalse(receipt['strategy_evaluated'])
        self.assertFalse(receipt['live_orders_enabled'])
        for field in ('model_fit_calls', 'model_predict_calls', 'policy_replays'):
            self.assertEqual(receipt[field], 0)
        serialized = vault.canonical(receipt)
        self.assertNotIn(b'content_key_b64', serialized)
        self.assertNotIn(b'synthetic-access-token', serialized)
        for kind in loader.INPUT_FILES:
            self.assertNotIn(self.caps[kind]['content_key_b64'].encode(), serialized)
            restored = self.bootstrap / kind
            self.assertEqual(restored.stat().st_mode & 0o777, 0o700)
            self.assertEqual(set(p.name for p in restored.iterdir()), set(self.raw[kind]) | {'dataset-manifest.json'})
            for name, value in self.raw[kind].items():
                self.assertEqual((restored / name).read_bytes(), value)
                self.assertEqual((restored / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(receipt['inputs'][kind]['descriptor_sha256'], self.config['private_inputs'][kind]['descriptor_sha256'])

    def assert_rejected_before_any_fetch(self, config=None, caps=None, **kwargs):
        fetch = mock.Mock(side_effect=AssertionError('network must not run'))
        with self.assertRaises((ValueError, TypeError, KeyError)):
            self.restore(config=config, caps=caps, fetch=fetch, **kwargs)
        fetch.assert_not_called()
        self.assertEqual(list(self.bootstrap.iterdir()), [])

    def test_either_bad_descriptor_or_csv_hash_prevents_all_fetches(self):
        for kind in loader.INPUT_FILES:
            with self.subTest(kind=kind, defect='descriptor'):
                config = copy.deepcopy(self.config)
                config['private_inputs'][kind]['descriptor_sha256'] = 'f'*64
                self.assert_rejected_before_any_fetch(config=config)
            for binding in loader.INPUT_FILES[kind].values():
                with self.subTest(kind=kind, defect=binding):
                    config = copy.deepcopy(self.config)
                    config['cache_bindings'][binding] = '0'*64
                    self.assert_rejected_before_any_fetch(config=config)

    def test_bad_control_capability_blocks_the_first_dataset_fetch(self):
        cases = []
        for value in ('', True, 'not-base64'):
            caps = copy.deepcopy(self.caps)
            caps['control']['content_key_b64'] = value
            cases.append(caps)
        caps = copy.deepcopy(self.caps); caps['control'] = copy.deepcopy(caps['dataset']); cases.append(caps)
        caps = copy.deepcopy(self.caps); caps['extra'] = caps['control']; cases.append(caps)
        for caps in cases:
            with self.subTest(control=caps.get('control')):
                self.assert_rejected_before_any_fetch(caps=caps)

    def test_immutable_current_repo_and_exact_episode_path_required(self):
        mutations = [('repository', 'outside/repository'), ('commit', 'main'),
            ('commit', 'B'*40), ('path', '../part-0000.vault'),
            ('path', 'research_vault/episodes/' + 'd'*64 + '/part-0001.vault'),
            ('path', 'https://example.invalid/data'), ('path', 'research_vault/episodes/' + 'd'*64 + '/../part-0000.vault')]
        for kind in loader.INPUT_FILES:
            for field, value in mutations:
                with self.subTest(kind=kind, field=field, value=value):
                    config = copy.deepcopy(self.config)
                    config['private_inputs'][kind]['source'][field] = value
                    self.assert_rejected_before_any_fetch(config=config)
        for code in ('main', 'A'*40, 'f'*40):
            with self.subTest(code=code):
                self.assert_rejected_before_any_fetch(code_commit=code)
        for repository in ('https://example.invalid/data', 'owner/..', '../repo', ''):
            with self.subTest(repository=repository):
                config = copy.deepcopy(self.config)
                for spec in config['private_inputs'].values():
                    spec['source']['repository'] = repository
                self.assert_rejected_before_any_fetch(config=config, repository=repository)

    def test_unexpected_file_and_input_schema_cannot_hide_in_descriptor(self):
        for kind in loader.INPUT_FILES:
            caps = copy.deepcopy(self.caps)
            next(entry for entry in caps[kind]['files'] if entry['path'].endswith('.csv'))['path'] = 'arbitrary.csv'
            caps[kind]['files'].sort(key=lambda entry: entry['path'])
            config = copy.deepcopy(self.config)
            descriptor = dataset.descriptor_from_capability(caps[kind])
            config['private_inputs'][kind]['descriptor_sha256'] = dataset.descriptor_sha256(descriptor)
            with self.subTest(kind=kind):
                self.assert_rejected_before_any_fetch(config=config, caps=caps)
        config = copy.deepcopy(self.config); config['private_inputs']['unexpected'] = {}
        self.assert_rejected_before_any_fetch(config=config)
        config = copy.deepcopy(self.config); config['private_inputs']['control']['url'] = 'https://example.invalid'
        self.assert_rejected_before_any_fetch(config=config)

    def test_existing_or_symlink_output_is_not_overwritten_and_never_fetched(self):
        for kind in loader.INPUT_FILES:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / kind
                output.mkdir()
                marker = output / 'marker'; marker.write_bytes(b'preserve')
                fetch = mock.Mock(side_effect=AssertionError('fetch forbidden'))
                with self.assertRaises(ValueError):
                    loader.restore_inputs(self.config, self.caps, root, repository=REPOSITORY, code_commit=CODE, fetch=fetch)
                fetch.assert_not_called(); self.assertEqual(marker.read_bytes(), b'preserve')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'outside'; target.mkdir()
            output = root / 'bootstrap'; output.symlink_to(target, target_is_directory=True)
            fetch = mock.Mock(side_effect=AssertionError('fetch forbidden'))
            with self.assertRaises(ValueError):
                loader.restore_inputs(self.config, self.caps, output, repository=REPOSITORY, code_commit=CODE, fetch=fetch)
            fetch.assert_not_called(); self.assertEqual(list(target.iterdir()), [])

    def test_corrupted_ciphertext_and_wrong_envelope_key_fail_without_success(self):
        for wrong in (self.envelopes['control'], self.envelopes['dataset'][:-1] + b'x'):
            with self.subTest(kind='bad-dataset'):
                fetch = mock.Mock(return_value=wrong)
                with self.assertRaises(ValueError): self.restore(fetch=fetch)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(list(self.bootstrap.iterdir()), [])
        caps = copy.deepcopy(self.caps)
        caps['dataset']['content_key_b64'] = self.caps['control']['content_key_b64']
        with self.assertRaises(ValueError): self.restore(caps=caps)
        self.assertEqual(list(self.bootstrap.iterdir()), [])

    def test_destination_created_during_fetch_is_exclusive_and_preserved(self):
        marker = self.bootstrap / 'dataset/marker'
        def competing_writer(source, maximum, **kwargs):
            marker.parent.mkdir(mode=0o700)
            marker.write_bytes(b'other-owner')
            return self.envelopes['dataset']
        with self.assertRaises(FileExistsError):
            self.restore(fetch=competing_writer)
        self.assertEqual(marker.read_bytes(), b'other-owner')
        self.assertFalse((self.bootstrap / 'dataset/features.csv').exists())
        self.assertFalse((self.bootstrap / 'control').exists())

    def test_second_fetch_failure_preserves_no_success_receipt_and_retry_cannot_overwrite(self):
        calls = 0
        def interrupted(source, maximum, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError('synthetic interruption')
            return self.envelopes['dataset']
        with self.assertRaises(TimeoutError): self.restore(fetch=interrupted)
        self.assertTrue((self.bootstrap / 'dataset/features.csv').is_file())
        self.assertFalse((self.bootstrap / 'control').exists())
        marker = (self.bootstrap / 'dataset/features.csv').read_bytes()
        fetch = mock.Mock(side_effect=AssertionError('ambiguous retry forbidden'))
        with self.assertRaises(ValueError): self.restore(fetch=fetch)
        fetch.assert_not_called()
        self.assertEqual((self.bootstrap / 'dataset/features.csv').read_bytes(), marker)

    def test_command_failure_writes_generic_keyless_receipt_exclusively(self):
        config = self.root / 'config.json'; config.write_bytes(vault.canonical(self.config))
        caps = {}
        for kind in loader.INPUT_FILES:
            caps[kind] = self.root / (kind + '-cap.json')
            caps[kind].write_bytes(vault.canonical(self.caps[kind]))
        receipt = self.root / 'receipt.json'
        arguments = ['loader', '--config', str(config), '--dataset-capability', str(caps['dataset']),
            '--control-capability', str(caps['control']), '--bootstrap', str(self.bootstrap), '--receipt', str(receipt)]
        with mock.patch.object(sys, 'argv', arguments), mock.patch.dict(os.environ,
                {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_SHA': CODE}), mock.patch.object(
                loader, 'restore_inputs', side_effect=ValueError(self.caps['control']['content_key_b64'])):
            self.assertEqual(loader.main(), 1)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        value = receipt.read_bytes()
        self.assertNotIn(self.caps['control']['content_key_b64'].encode(), value)
        self.assertNotIn(b'content_key_b64', value)
        self.assertEqual(json.loads(value)['status'], 'failed')
        with mock.patch.object(sys, 'argv', arguments), mock.patch.dict(os.environ,
                {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_SHA': CODE}), mock.patch.object(loader, 'restore_inputs', return_value={}):
            with self.assertRaises(FileExistsError): loader.main()
        self.assertEqual(receipt.read_bytes(), value)


if __name__ == '__main__':
    unittest.main()
