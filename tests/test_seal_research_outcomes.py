"""Tiny recipient-encrypted delivery tests; never fit, scan a tape or call GitHub."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools import research_vault as vault
from tools import seal_research_outcomes as delivery
from tools import persist_quality_first_journal as journal


class SealResearchOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = self.root / 'private'; self.private.mkdir(mode=0o700)
        (self.private / 'results').mkdir(mode=0o700)
        self.files = {'frozen_input.json': b'{"fixed_protocol":true}',
            'diagnostics.txt': b'{"passed":true,"checks":3}',
            'results/summary.json': b'{"strategy_evaluated":true,"comparison_valid":true}',
            'results/scores.csv': b'market_id,score\nsynthetic,0.25\n'}
        for name, value in self.files.items():
            (self.private / name).write_bytes(value)
        self.key = vault.X25519PrivateKey.generate()
        self.recipient = vault.recipient_for(self.key)
        self.target = self.root / 'sealed-delivery'
        self.metadata = {'strategy_evaluated': True, 'comparison_valid': True, 'actual_model_fit_calls': 2,
            'workflow': {'EVALUATION_OUTCOME': 'success', 'INDEPENDENT_OUTCOME': 'success', 'INPUT_OUTCOME': 'success'},
            'live_orders_enabled': False, 'actual_fills': 0, 'verified_canary_fills': 0}

    def seal(self, **kwargs):
        return delivery.seal_outcomes(self.private, self.target, self.recipient,
            self.recipient['recipient_id'], metadata=self.metadata,
            max_member_bytes=256 * 1024 ** 2, **kwargs)

    def recovered(self):
        status = json.loads((self.target / 'status.json').read_text())
        parts = [json.loads(vault.unseal(path.read_bytes(), self.key)) for path in sorted(self.target.glob('part-*.vault'))]
        packed = b''.join(base64.b64decode(part['payload_base64']) for part in parts)
        self.assertTrue(all(part['bundle_sha256'] == hashlib.sha256(packed).hexdigest() for part in parts))
        self.assertEqual(status['sealed_parts'], len(parts))
        with tarfile.open(fileobj=io.BytesIO(packed), mode='r:gz') as archive:
            files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
        self.assertEqual(set(path.name for path in self.target.iterdir()),
            {'status.json'} | {f'part-{i:04d}.vault' for i in range(len(parts))})
        self.assertFalse(status['plaintext_published'])
        return status, files

    def test_full_delivery_preserves_complete_sources_and_explicit_member_opt_in(self):
        calls = []
        def pack(path, **kwargs):
            calls.append((path, kwargs))
            return delivery.bundle.make_bundle(path, **kwargs)
        result = self.seal(make_bundle=pack)
        self.assertTrue(result['delivery_complete']); self.assertTrue(result['full_evidence_retained'])
        self.assertEqual(calls, [(self.private, {'max_member_bytes': 256 * 1024 ** 2})])
        status, files = self.recovered()
        self.assertEqual(set(files), set(self.files) | {'run.json'})
        metadata = json.loads(files['run.json'])
        self.assertTrue(metadata['strategy_evaluated']); self.assertTrue(metadata['full_evidence_retained'])
        self.assertEqual(metadata['delivery_mode'], 'full')
        self.assertTrue(status['strategy_evaluated'])
        self.assertEqual((self.private / 'results/scores.csv').read_bytes(), self.files['results/scores.csv'])

    def test_three_known_size_errors_retain_small_evidence_and_never_claim_full_result(self):
        for error in sorted(delivery.SIZE_FAILURES):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); private = root / 'private'; private.mkdir(); (private / 'results').mkdir()
                for name, raw in self.files.items(): (private / name).write_bytes(raw)
                calls = []
                def pack(path, **kwargs):
                    calls.append(path)
                    if len(calls) == 1: raise ValueError(error)
                    return delivery.bundle.make_bundle(path, **kwargs)
                target = root / 'sealed-delivery'
                result = delivery.seal_outcomes(private, target, self.recipient, self.recipient['recipient_id'],
                    metadata=self.metadata, max_member_bytes=256 * 1024 ** 2, make_bundle=pack)
                self.assertTrue(result['delivery_complete']); self.assertFalse(result['full_evidence_retained'])
                self.assertFalse(result['strategy_evaluated'])
                old_target = self.target; self.target = target
                status, files = self.recovered(); self.target = old_target
                self.assertEqual(set(files), {'run.json', 'frozen_input.json', 'diagnostics.txt',
                    'results/summary.json', 'results/delivery-failure.json'})
                metadata, manifest = json.loads(files['run.json']), json.loads(files['results/delivery-failure.json'])
                inspected = journal.inspect_delivery(target, self.recipient['recipient_id'])
                self.assertEqual(inspected['status'], status)
                self.assertFalse(metadata['strategy_evaluated']); self.assertFalse(metadata['comparison_valid'])
                self.assertFalse(metadata['full_evidence_retained']); self.assertFalse(status['strategy_evaluated'])
                self.assertEqual(metadata['analysis_step_outcome'], 'success')
                self.assertEqual(metadata['independent_step_outcome'], 'success')
                self.assertEqual(manifest['delivery_failure']['code'], error)
                self.assertFalse(manifest['large_derived_artifacts_copied'])
                original = next(item for item in manifest['original_files'] if item['path'] == 'results/scores.csv')
                self.assertEqual(original['bytes'], len(self.files['results/scores.csv']))
                self.assertEqual(original['sha256'], hashlib.sha256(self.files['results/scores.csv']).hexdigest())
                self.assertEqual(files['results/summary.json'], self.files['results/summary.json'])
                self.assertEqual(files['diagnostics.txt'], self.files['diagnostics.txt'])
                self.assertTrue((private / 'results/scores.csv').exists())
                self.assertFalse(list(root.glob('.compact-evidence-*')))
                self.assertFalse(list(root.glob('.sealed-stage-*')))

    def test_non_size_error_does_not_disguise_failure_as_compact_success(self):
        for error in ('RESULT_SYMLINK', 'RESULT_CHANGED', 'RESULT_NOT_REGULAR', 'BUNDLE_BUDGET'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); private = root / 'private'; private.mkdir()
                pack = mock.Mock(side_effect=ValueError(error))
                with self.assertRaisesRegex(ValueError, error):
                    delivery.seal_outcomes(private, root / 'delivery', self.recipient,
                        self.recipient['recipient_id'], metadata=self.metadata, make_bundle=pack)
                self.assertEqual(pack.call_count, 1)
                self.assertFalse((root / 'delivery').exists())
                self.assertFalse(list(root.glob('.sealed-stage-*')))

    def test_partial_ciphertext_failure_is_not_published_or_marked_complete(self):
        def partial(packed, output, recipient, rid):
            (output / 'part-0000.vault').write_bytes(b'incomplete-ciphertext')
            raise OSError('synthetic partial write')
        with self.assertRaisesRegex(OSError, 'synthetic partial write'):
            self.seal(deliver_bundle=partial)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('.sealed-stage-*')))
        self.assertTrue((self.private / 'results/scores.csv').exists())

    def test_incomplete_part_set_is_not_published(self):
        def missing(packed, output, recipient, rid):
            vault.write_new(output / 'part-0000.vault', vault.seal(b'synthetic', recipient, rid))
            return 2
        with self.assertRaisesRegex(ValueError, 'SEALED_PART_SET_INCOMPLETE'):
            self.seal(deliver_bundle=missing)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('.sealed-stage-*')))

    def test_existing_delivery_is_exclusive_and_preserved(self):
        self.target.mkdir(); marker = self.target / 'marker'; marker.write_bytes(b'preserve')
        pack = mock.Mock(side_effect=AssertionError('no packing on duplicate'))
        with self.assertRaises(FileExistsError): self.seal(make_bundle=pack)
        pack.assert_not_called()
        self.assertEqual(marker.read_bytes(), b'preserve')
        self.assertFalse((self.private / 'run.json').exists())

    def test_compact_copy_cannot_follow_a_parent_symlink(self):
        for path in (self.private / 'results').iterdir(): path.unlink()
        (self.private / 'results').rmdir()
        outside = self.root / 'outside'; outside.mkdir()
        marker = outside / 'summary.json'; marker.write_bytes(b'{"outside":"never copy"}')
        (self.private / 'results').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.seal(make_bundle=mock.Mock(side_effect=ValueError('RESULT_SIZE_LIMIT')))
        self.assertFalse(self.target.exists())
        self.assertEqual(marker.read_bytes(), b'{"outside":"never copy"}')
        self.assertFalse(list(self.root.glob('.compact-evidence-*')))

    def test_oversized_compact_record_is_named_as_omitted_without_copying(self):
        calls = 0
        def pack(path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1: raise ValueError('RESULT_SIZE_LIMIT')
            return delivery.bundle.make_bundle(path, **kwargs)
        with mock.patch.object(delivery, 'MAX_COMPACT_MEMBER_BYTES', 32):
            result = self.seal(make_bundle=pack)
        self.assertFalse(result['full_evidence_retained'])
        _, files = self.recovered()
        manifest = json.loads(files['results/delivery-failure.json'])
        self.assertNotIn('results/summary.json', files)
        self.assertIn({'path': 'results/summary.json', 'reason': 'compact_member_limit'}, manifest['compact_omitted_small_records'])

    def test_invalid_member_budget_rejected_before_output_creation(self):
        for value in (True, 0, 256 * 1024 ** 2 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delivery.seal_outcomes(self.private, self.target, self.recipient, self.recipient['recipient_id'],
                    metadata=self.metadata, max_member_bytes=value)
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
