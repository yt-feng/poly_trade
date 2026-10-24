"""Synthetic sealed journal persistence contracts; no keys/accounts/network."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import persist_quality_first_journal as journal
import research_vault
import research_journal_guard
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


class SealedJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'delivery'
        self.source.mkdir()
        self.episodes = self.root / 'research_vault' / 'episodes'
        self.private = X25519PrivateKey.generate()
        self.recipient = research_vault.recipient_for(self.private)
        self.rid = self.recipient['recipient_id']
        self.status = {'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
                       'strategy_evaluated': True, 'live_orders_enabled': False,
                       'plaintext_published': False, 'sealed_parts': 2}
        self.write_status()
        for index in range(2):
            ciphertext = research_vault.seal(f'synthetic private research {index}'.encode(), self.recipient, self.rid)
            # Noncanonical whitespace is permitted but must survive byte for byte.
            (self.source / f'part-{index:04d}.vault').write_bytes(b'\n' + ciphertext + b'\n')

    def tearDown(self):
        self.temporary.cleanup()

    def write_status(self):
        (self.source / 'status.json').write_text(json.dumps(self.status, indent=2))

    def persist(self):
        return journal.persist_delivery(self.source, self.episodes, 'example/research', '123456', '1', self.rid)

    def test_ciphertext_exact_copy_and_idempotent_run_identity(self):
        first = self.persist()
        self.assertEqual(first['status'], 'persisted')
        target = self.episodes / first['episode_id']
        self.assertEqual(len(first['episode_id']), 64)
        self.assertEqual({p.name for p in target.iterdir()}, {'status.json', 'part-0000.vault', 'part-0001.vault'})
        for name in ['part-0000.vault', 'part-0001.vault']:
            self.assertEqual((target / name).read_bytes(), (self.source / name).read_bytes())
            self.assertNotIn(b'synthetic private research', (target / name).read_bytes())
        self.assertEqual(json.loads((target / 'status.json').read_text()), self.status)
        self.assertEqual(self.persist()['status'], 'already_present')
        self.assertEqual(len(list(self.episodes.iterdir())), 1)
        self.assertEqual(journal.episode_identity('Example/Research', 123456, 1), first['episode_id'])
        self.assertNotEqual(journal.episode_identity('example/research', 123456, 2), first['episode_id'])

    def test_existing_run_cannot_be_overwritten_with_different_valid_ciphertext(self):
        target = self.episodes / self.persist()['episode_id']
        original = (target / 'part-0000.vault').read_bytes()
        (self.source / 'part-0000.vault').write_bytes(research_vault.seal(b'changed', self.recipient, self.rid))
        with self.assertRaisesRegex(ValueError, 'DIFFERENT_DELIVERY'):
            self.persist()
        self.assertEqual((target / 'part-0000.vault').read_bytes(), original)

    def test_unknown_files_directories_and_links_are_rejected_without_destination(self):
        for name in ['research.json', 'private.key', 'notes.txt', '.DS_Store']:
            extra = self.source / name
            extra.write_text('SYNTHETIC-PRIVATE-TEXT')
            with self.assertRaises(ValueError):
                self.persist()
            extra.unlink()
        nested = self.source / 'nested'
        nested.mkdir()
        with self.assertRaises(ValueError):
            self.persist()
        nested.rmdir()
        part = self.source / 'part-0000.vault'
        original = part.read_bytes()
        part.unlink()
        external = self.root / 'external.vault'
        external.write_bytes(original)
        part.symlink_to(external)
        with self.assertRaises(ValueError):
            self.persist()
        self.assertFalse(self.episodes.exists())

    def test_wrong_fingerprint_plaintext_and_unknown_envelope_fields_rejected(self):
        with self.assertRaisesRegex(ValueError, 'RECIPIENT_MISMATCH'):
            journal.inspect_delivery(self.source, '0' * 64)
        path = self.source / 'part-0000.vault'
        original = path.read_bytes()
        for body in [b'{"research":"SYNTHETIC-PRIVATE-TEXT"}',
                     research_vault.canonical(dict(json.loads(original), research='SYNTHETIC-PRIVATE-TEXT')),
                     b'{"version":1,"version":1}']:
            path.write_bytes(body)
            with self.assertRaises((ValueError, TypeError)):
                self.persist()
        self.assertFalse(self.episodes.exists())

    def test_part_gaps_count_mismatch_and_zero_parts_rejected(self):
        (self.source / 'part-0001.vault').rename(self.source / 'part-0002.vault')
        with self.assertRaisesRegex(ValueError, 'SEQUENCE'):
            self.persist()
        (self.source / 'part-0002.vault').rename(self.source / 'part-0001.vault')
        for count in [0, 1, 3, 17, True, 2.0]:
            self.status['sealed_parts'] = count
            self.write_status()
            with self.assertRaises(ValueError):
                self.persist()

    def test_status_rejects_metrics_free_text_booleans_and_inconsistent_states(self):
        base = copy.deepcopy(self.status)
        changes = [{'private_finding': 'SYNTHETIC-PRIVATE-TEXT'}, {'version': True},
                   {'strategy_evaluated': 1},
                   {'live_orders_enabled': True}, {'plaintext_published': True},
                   {'stage': 'research'}, {'failed_stage': 'research'},
                   {'status': 'failed'}, {'status': 'failed', 'strategy_evaluated': False, 'failed_stage': 'arbitrary text'}]
        for update in changes:
            self.status = dict(base, **update)
            self.write_status()
            with self.assertRaises(ValueError):
                self.persist()
        self.assertFalse(self.episodes.exists())

    def test_failed_research_with_sealed_diagnostics_is_persistable(self):
        self.status.update(status='failed', strategy_evaluated=False, failed_stage='research')
        self.write_status()
        result = self.persist()
        self.assertEqual(result['status'], 'persisted')
        stored = json.loads((self.episodes / result['episode_id'] / 'status.json').read_text())
        self.assertEqual(stored['failed_stage'], 'research')
        self.assertFalse(stored['strategy_evaluated'])

    def test_failed_acquisition_accepts_only_bounded_failure_code(self):
        self.status.update(status='failed', strategy_evaluated=False,
                           failed_stage='acquisition', failure_code='remote_transport_failed')
        self.write_status()
        result = self.persist()
        stored = json.loads((self.episodes / result['episode_id'] / 'status.json').read_text())
        self.assertEqual(stored['failure_code'], 'remote_transport_failed')
        self.status['failure_code'] = 'host-and-secret-details'
        self.write_status()
        with self.assertRaisesRegex(ValueError, 'FAILURE_CODE'):
            self.persist()

    def test_manual_work_summary_uses_same_envelope_without_claiming_evaluation(self):
        self.status['strategy_evaluated'] = False
        self.write_status()
        bundle = journal.validate_delivery(self.source, self.rid)
        self.assertEqual(set(bundle), {'part-0000.vault', 'part-0001.vault', 'status.json'})
        self.assertFalse(json.loads(bundle['status.json'])['strategy_evaluated'])
        self.assertEqual(bundle['part-0000.vault'], (self.source / 'part-0000.vault').read_bytes())
        self.assertEqual(self.persist()['status'], 'persisted')

    def test_identity_rejects_paths_free_text_and_boolean_ids(self):
        for repo in ['../research', 'owner/..', 'owner/repo/nested', 'https://example/research', 'owner/research\nprivate']:
            with self.assertRaises(ValueError):
                journal.episode_identity(repo, '123', '1')
        for bad in [True, False, 0, -1, 1.0, '', '001', '../private', '1\nprivate']:
            with self.assertRaises(ValueError):
                journal.episode_identity('example/research', bad, '1')

    def test_source_mutation_during_copy_is_rejected_and_staging_cleaned(self):
        original = journal.read_regular
        calls = 0
        def changed(path, limit):
            nonlocal calls
            if Path(path).name == 'part-0000.vault':
                calls += 1
                if calls == 2:
                    Path(path).write_bytes(research_vault.seal(b'changed after validation', self.recipient, self.rid))
            return original(path, limit)
        with patch.object(journal, 'read_regular', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'CHANGED'):
                self.persist()
        self.assertEqual(list(self.episodes.iterdir()), [])

    def test_generic_publication_guard_checks_episode_paths_and_status_values(self):
        persisted = self.persist()
        prefix = f"research_vault/episodes/{persisted['episode_id']}/"
        names = [prefix + name for name in ['status.json', 'part-0000.vault', 'part-0001.vault']]
        self.assertTrue(research_journal_guard.audit_paths(names, self.root)['passed'])
        for name in [prefix + 'research.json', prefix + 'notes.vault', prefix + 'nested/status.json',
                     'research_vault/episodes/plaintext-experiment-name/status.json',
                     (prefix + 'status.json').replace('/', '\\')]:
            self.assertFalse(research_journal_guard.allowed_new_path(name))
        target = self.root / prefix / 'status.json'
        bad = dict(self.status, empirical_result='SYNTHETIC-PRIVATE-TEXT')
        target.write_text(json.dumps(bad))
        report = research_journal_guard.audit_paths([prefix + 'status.json'], self.root)
        self.assertFalse(report['passed'])
        self.assertEqual(report['reason_codes'], ['INVALID_EPISODE_STATUS'])

    def test_legacy_episode_name_is_byte_for_byte_unchanged(self):
        expected = '1a14da7c34e7847ecd72ea0f70bd2b40848bf20c56013a0131acd79c4c8e59b4'
        self.assertEqual(journal.episode_identity('yt-feng/poly_trade', '123456789', '2'), expected)
        self.assertEqual(journal.episode_identity('YT-FENG/poly_trade', 123456789, 2, cell_id=None), expected)

    def test_cell_delivery_isolation_idempotency_and_conflict(self):
        legacy = self.persist()
        def cell(label):
            return journal.persist_delivery(self.source, self.episodes, 'example/research',
                                            '123456', '1', self.rid, cell_id=label)
        first = cell('h373-crossing-0.0')
        original = (self.source / 'part-0000.vault').read_bytes()
        (self.source / 'part-0000.vault').write_bytes(research_vault.seal(b'synthetic second cell', self.recipient, self.rid))
        second = cell('h373-crossing-0.25')
        self.assertEqual(cell('h373-crossing-0.25')['status'], 'already_present')
        self.assertEqual(len({legacy['episode_id'], first['episode_id'], second['episode_id']}), 3)
        for result in (legacy, first):
            self.assertEqual((self.episodes / result['episode_id'] / 'part-0000.vault').read_bytes(), original)
        (self.source / 'part-0000.vault').write_bytes(research_vault.seal(b'changed same cell', self.recipient, self.rid))
        with self.assertRaisesRegex(ValueError, 'DIFFERENT_DELIVERY'):
            cell('h373-crossing-0.25')
        self.assertEqual(len(list(self.episodes.iterdir())), 3)

    def test_cell_identity_rejects_invalid_labels_before_writing(self):
        for bad in [True, 0, 1.0, '', '.', '..', '../x', 'a/b', 'a\\b', 'a:b',
                    'a\n', 'a b', 'é', 'a' * 65, {}, []]:
            with self.subTest(cell=bad), self.assertRaisesRegex(ValueError, 'INVALID_CELL_IDENTITY'):
                journal.persist_delivery(self.source, self.episodes, 'example/research',
                                         '123456', '1', self.rid, cell_id=bad)
            self.assertFalse(self.episodes.exists())

    def test_cell_identity_keeps_run_dimensions_and_exact_labels(self):
        base = journal.episode_identity('example/research', '123456', '1', cell_id='0.25')
        self.assertEqual(base, journal.episode_identity('Example/Research', 123456, 1, cell_id='0.25'))
        others = [
            journal.episode_identity('example/research', '123456', '1'),
            journal.episode_identity('example/research', '123456', '1', cell_id='0.250'),
            journal.episode_identity('example/research', '123456', '2', cell_id='0.25'),
            journal.episode_identity('example/research', '123457', '1', cell_id='0.25'),
            journal.episode_identity('example/other', '123456', '1', cell_id='0.25'),
            journal.episode_identity('example/research', '123456', '1', cell_id='a' * 64),
        ]
        self.assertEqual(len(set([base] + others)), 7)

    def test_cli_cell_opt_in_and_legacy_default(self):
        identities = []
        for label in (None, 'h373-crossing-0.25'):
            argv = ['persist', '--input', str(self.source), '--episodes-dir', str(self.episodes),
                    '--repository', 'example/research', '--run-id', '123456',
                    '--run-attempt', '1', '--recipient-id', self.rid]
            if label is not None:
                argv += ['--cell-id', label]
            for expected in ('persisted', 'already_present'):
                output = io.StringIO()
                with patch.object(sys, 'argv', argv), redirect_stdout(output):
                    self.assertEqual(journal.main(), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result['status'], expected)
                if label:
                    self.assertNotIn(label, output.getvalue())
            identities.append(result['episode_id'])
        self.assertNotEqual(*identities)
        self.assertEqual({p.name for p in self.episodes.iterdir()}, set(identities))


if __name__ == '__main__':
    unittest.main()
