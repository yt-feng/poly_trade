"""Synthetic tests: no public feed, personal identity, wallet or order access."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_quality_first_research as runner
import research_vault
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def item(day, hour, size=10, prefix='snapshots'):
    return {'tag': 'capture-v2-1-1', 'name': f'{prefix}-{day}-{hour:02d}0000.jsonl.gz',
            'bytes': size, 'selected': False, 'pre_error': None}


class QualityFirstRunnerTests(unittest.TestCase):
    def setUp(self):
        self.private = X25519PrivateKey.generate()
        self.recipient = research_vault.recipient_for(self.private)
        self.rid = self.recipient['recipient_id']

    def test_stratification_reserves_each_day_and_spreads_intraday(self):
        items = [item(f'2026-10-{day:02d}', hour) for day in range(1, 9) for hour in range(24)]
        plan = runner.stratify_plan({'items': items, 'max_bytes': 640}, 64)
        stats = plan['research_selection']
        self.assertEqual(set(stats['selected_snapshot_archives_by_day'].values()), {8})
        self.assertEqual(plan['reserved_bytes'], 640)
        chosen = [i['name'] for i in items if i['selected'] and '2026-10-01' in i['name']]
        hours = [int(name.rsplit('-', 1)[1][:2]) for name in chosen]
        self.assertLess(min(hours), 6)
        self.assertGreater(max(hours), 18)
        self.assertFalse(stats['prospective_evidence'])

    def test_pairs_are_atomic_and_oversized_newest_day_cannot_take_older_quota(self):
        items = [item('2026-10-01', h) for h in range(8)]
        items += [item('2026-10-01', h, 2, 'labels') for h in range(8)]
        items += [item('2026-10-02', h, 101) for h in range(8)]
        plan = runner.stratify_plan({'items': items, 'max_bytes': 200}, 16)
        self.assertEqual(plan['research_selection']['selected_snapshot_archives_by_day'], {'2026-10-01': 8, '2026-10-02': 0})
        self.assertEqual(sum(i['bytes'] for i in items if i['selected']), 96)
        self.assertEqual(len([i for i in items if i['selected']]), 16)

    def test_only_unselected_source_errors_move_to_retained_exclusion_provenance(self):
        good, bad = item('2026-10-01', 0), item('2026-10-01', 1)
        good['selected'] = True
        bad['pre_error'] = 'missing checksum sidecar'
        plan = {'items': [good, bad], 'max_releases': 16, 'max_bytes': 1024,
                'releases': [{'tag': good['tag'], 'eligible_snapshot_label_archives': 2}]}
        runner.record_unselected_source_issues(plan)
        self.assertEqual(plan['excluded_source_issues'][0]['source_issue'], 'missing checksum sidecar')
        self.assertEqual(plan['excluded_source_issues'][0]['name'], bad['name'])
        self.assertIsNone(bad['pre_error'])
        state = {good['tag'] + '/' + good['name']: {'status': 'verified', 'file': {'name': good['name'], 'bytes': good['bytes']}}}
        manifest = runner.forward_acquire._manifest(plan, state, 0)
        self.assertTrue(runner.forward_acquire.validate_manifest(manifest))
        self.assertEqual(len(manifest['skipped']), 1)
        # A defect on an actual selected item must still fail the same gate.
        bad.update(selected=True, pre_error='selected checksum missing')
        runner.record_unselected_source_issues(plan)
        self.assertEqual(bad['pre_error'], 'selected checksum missing')
        manifest = runner.forward_acquire._manifest(plan, state, 0)
        self.assertFalse(runner.forward_acquire.validate_manifest(manifest))
        self.assertEqual(manifest['errors'][0]['error'], 'selected checksum missing')

    def test_recipient_is_verified_before_any_work(self):
        encoded = base64.b64encode(research_vault.canonical(self.recipient)).decode()
        self.assertEqual(runner.load_recipient(encoded, self.rid), self.recipient)
        with self.assertRaises(ValueError):
            runner.load_recipient(encoded, '0' * 64)
        wrong = dict(self.recipient, private_key='never-accepted')
        with self.assertRaises(ValueError):
            runner.load_recipient(base64.b64encode(json.dumps(wrong).encode()).decode(), self.rid)

    def test_cached_data_requires_fresh_source_checksum_and_local_rehash(self):
        url = 'https://github.com/yt-feng/poly/releases/download/capture-v2-1-1/snapshots-2026-10-01-000000.jsonl.gz'
        body = b'synthetic archive bytes'
        side = (hashlib.sha256(body).hexdigest() + '  snapshots-2026-10-01-000000.jsonl.gz').encode()
        calls = []
        def request(target, **kwargs):
            calls.append(target)
            return side if target.endswith('.sha256') else body
        with tempfile.TemporaryDirectory() as directory, patch.object(runner.forward_acquire, 'request', side_effect=request):
            for _ in range(2):
                with runner.acquisition_adapters(directory, 8):
                    runner.forward_acquire.request(url + '.sha256')
                    self.assertEqual(runner.forward_acquire.request(url), body)
            self.assertEqual(calls.count(url + '.sha256'), 2)
            self.assertEqual(calls.count(url), 1)
            cache = next(Path(directory).glob('*.blob'))
            cache.write_bytes(b'corrupt')
            with runner.acquisition_adapters(directory, 8):
                runner.forward_acquire.request(url + '.sha256')
                self.assertEqual(runner.forward_acquire.request(url), body)
            self.assertEqual(calls.count(url), 2)

    def test_bundle_envelopes_round_trip_without_plaintext_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, output = root / 'private', root / 'delivery'
            private.mkdir(); output.mkdir()
            (private / 'results').mkdir()
            marker = 'SYNTHETIC-EMPIRICAL-RESULT-MUST-BE-SEALED'
            (private / 'results/model.json').write_text(json.dumps({'fixture': marker}))
            (private / 'run.json').write_text('{}')
            bundle = runner.make_bundle(private)
            with patch.object(runner, 'PART_BYTES', 80):
                parts = runner.deliver_bundle(bundle, output, self.recipient, self.rid)
            reconstructed = []
            for index, path in enumerate(sorted(output.glob('*.vault'))):
                self.assertNotIn(marker.encode(), path.read_bytes())
                payload = json.loads(research_vault.unseal(path.read_bytes(), self.private))
                self.assertEqual((payload['index'], payload['total']), (index, parts))
                reconstructed.append(base64.b64decode(payload['payload_base64']))
            self.assertEqual(b''.join(reconstructed), bundle)
            with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
                self.assertIn(marker, archive.extractfile('results/model.json').read().decode())
            self.assertTrue(all(p.suffix == '.vault' for p in output.iterdir()))

    def test_research_errors_are_sealed_and_generic_status_is_public(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'delivery'
            args = argparse.Namespace(recipient_json_b64=base64.b64encode(research_vault.canonical(self.recipient)).decode(),
                recipient_id=self.rid, output=str(output), cache=None, max_releases=64, max_bytes=1024**3,
                max_snapshot_archives=160, initial_cash=10, timeout_seconds=10)
            manifest = {'errors': []}
            public = io.StringIO()
            marker = 'SYNTHETIC-PRIVATE-DIAGNOSTIC'
            with patch.object(runner.forward_acquire, 'acquire', return_value=manifest), \
                 patch.object(runner.forward_acquire, 'validate_manifest', return_value=True), \
                 patch.object(runner.subprocess, 'run', side_effect=RuntimeError(marker)), contextlib.redirect_stdout(public):
                self.assertEqual(runner.run(args), 2)
            self.assertNotIn(marker, public.getvalue())
            self.assertNotIn(marker, (output / 'status.json').read_text())
            envelope = next(output.glob('*.vault')).read_bytes()
            payload = json.loads(research_vault.unseal(envelope, self.private))
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload['payload_base64'])), mode='r:gz') as archive:
                self.assertIn(marker, archive.extractfile('diagnostics.txt').read().decode())

    def test_frozen_model_is_forwarded_privately_and_child_has_no_github_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / 'existing-model.json'
            frozen.write_text(json.dumps({'model': None, 'sha256': 'synthetic', 'forward_min_window_start_ms': 1}))
            args = argparse.Namespace(recipient_json_b64=base64.b64encode(research_vault.canonical(self.recipient)).decode(),
                recipient_id=self.rid, output=str(root / 'delivery'), cache=None, max_releases=64, max_bytes=1024**3,
                max_snapshot_archives=96, initial_cash=10, timeout_seconds=10, frozen_model=frozen)
            def child(command, **kwargs):
                self.assertNotIn('GH_TOKEN', kwargs['env'])
                delivered_model = Path(command[command.index('--frozen-model') + 1])
                self.assertNotEqual(delivered_model, frozen)
                self.assertEqual(delivered_model.read_bytes(), frozen.read_bytes())
                result = Path(command[command.index('--output') + 1])
                result.mkdir()
                (result / 'research.json').write_text('{"fixture":true}')
                return argparse.Namespace(returncode=0)
            with patch.object(runner.forward_acquire, 'acquire', return_value={'errors': []}), \
                 patch.object(runner.forward_acquire, 'validate_manifest', return_value=True), \
                 patch.object(runner.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.run(args), 0)


if __name__ == '__main__':
    unittest.main()
