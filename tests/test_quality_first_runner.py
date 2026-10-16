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
import quality_first_research as research
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def item(day, hour, size=10, prefix='snapshots'):
    return {'tag': 'capture-v2-1-1', 'name': f'{prefix}-{day}-{hour:02d}0000.jsonl.gz',
            'bytes': size, 'selected': False, 'pre_error': None}


def timestamp(iso):
    return int(runner.datetime.fromisoformat(iso).timestamp() * 1000)


def challenger_fixture():
    cutoff = timestamp('2026-10-15T11:55:00+00:00')
    return research.research_challenger_payload(
        {'family': 'spot_residual', 'fit_cutoff_ms': cutoff}, 'synthetic-source', cutoff,
        10, timestamp('2026-10-15T12:03:00+00:00'))


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

    def test_frozen_episode_gets_two_days_192_complete_shards_in_same_byte_budget(self):
        bounds = runner.frozen_episode_bounds(challenger_fixture(), 10)
        def source():
            items = [item(f'2026-10-{day:02d}', index, 4 * 1024**2)
                     for day in range(5, 17) for index in range(96)]
            items += [item(f'2026-10-{day:02d}', index, 1024, 'labels')
                      for day in range(5, 17) for index in range(96)]
            return {'items': items, 'max_bytes': 1024**3}
        plan = runner.stratify_plan(source(), 192, bounds)
        stats = plan['research_selection']
        self.assertEqual(stats['selection'], 'frozen_episode_utc_days')
        self.assertEqual(stats['selected_snapshot_archives_by_day'], {'2026-10-15': 96, '2026-10-16': 96})
        self.assertEqual(stats['per_day_byte_quota'], 512 * 1024**2)
        self.assertEqual(len(stats['eligible_source_utc_days']), 12)
        self.assertEqual(stats['missing_utc_days'], [])
        self.assertTrue(stats['partial_coverage'])
        chosen = [entry for entry in plan['items'] if entry['selected']]
        self.assertEqual(len(chosen), 384)  # Snapshot and paired label are indivisible.
        self.assertEqual(plan['reserved_bytes'], 192 * (4 * 1024**2 + 1024))
        self.assertLessEqual(plan['reserved_bytes'], plan['max_bytes'])
        self.assertTrue(all('2026-10-15' in entry['name'] or '2026-10-16' in entry['name'] for entry in chosen))
        # Discovery retains the original 96-shard spread across all 12 days.
        discovery = runner.stratify_plan(source())['research_selection']
        self.assertEqual(set(discovery['selected_snapshot_archives_by_day'].values()), {8})
        self.assertEqual(discovery['observation_scope'], 'retrospective_recent_public_archives')
        self.assertNotIn('selection', discovery)
        # Explicit smaller caller limits are never enlarged by frozen mode.
        limited = runner.stratify_plan(source(), 8, bounds)['research_selection']
        self.assertEqual(limited['selected_snapshot_archives_by_day'], {'2026-10-15': 4, '2026-10-16': 4})

    def test_frozen_episode_midnight_end_excludes_next_day_and_missing_day_keeps_quota(self):
        begin = timestamp('2026-10-15T00:00:00+00:00')
        end = timestamp('2026-10-16T00:00:00+00:00')
        self.assertEqual(runner.frozen_episode_utc_days((begin, end)), ['2026-10-15'])
        plan = runner.stratify_plan({'items': [item('2026-10-15', i) for i in range(8)]
                                   + [item('2026-10-16', i) for i in range(8)], 'max_bytes': 160}, 16, (begin, end))
        self.assertEqual(plan['research_selection']['available_utc_days'], ['2026-10-15'])
        bounds = runner.frozen_episode_bounds(challenger_fixture(), 10)
        partial = runner.stratify_plan({'items': [item('2026-10-15', i) for i in range(16)],
                                       'max_bytes': 160}, 16, bounds)['research_selection']
        self.assertEqual(partial['per_day_byte_quota'], 80)
        self.assertEqual(partial['selected_snapshot_archives_by_day'], {'2026-10-15': 8, '2026-10-16': 0})
        self.assertEqual(partial['missing_utc_days'], ['2026-10-16'])

    def test_frozen_scope_requires_existing_engine_integrity_protocol_cash_and_24h_contract(self):
        frozen = challenger_fixture()
        self.assertEqual(runner.frozen_episode_bounds(frozen, 10),
                         (frozen['forward_min_window_start_ms'], frozen['forward_max_window_end_ms']))
        self.assertIn('sha256', frozen)  # Input identity is not mutated.
        with self.assertRaisesRegex(ValueError, 'INTEGRITY'):
            runner.frozen_episode_bounds(dict(frozen, sha256='0' * 64), 10)
        with self.assertRaisesRegex(ValueError, 'CASH'):
            runner.frozen_episode_bounds(frozen, 11)
        for field, value, reason in [('protocol_sha256', '0' * 64, 'PROTOCOL'),
                                      ('forward_max_window_end_ms', frozen['forward_max_window_end_ms'] + 1000, 'bounds')]:
            changed = dict(frozen)
            changed.pop('sha256'); changed.pop('episode_id')
            changed[field] = value
            changed['episode_id'] = research.digest(changed)
            changed['sha256'] = research.digest(changed)
            with self.assertRaisesRegex(ValueError, reason):
                runner.frozen_episode_bounds(changed, 10)
        self.assertIsNone(runner.frozen_episode_bounds({'kind': None}, 10))

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

    def test_no_episode_source_dates_fail_sealed_without_historical_fallback_or_research(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / 'frozen.json'
            frozen.write_text(json.dumps(challenger_fixture()))
            output = root / 'delivery'
            args = argparse.Namespace(recipient_json_b64=base64.b64encode(research_vault.canonical(self.recipient)).decode(),
                recipient_id=self.rid, output=str(output), cache=None, max_releases=64, max_bytes=1024**3,
                max_snapshot_archives=192, initial_cash=10, timeout_seconds=10, frozen_model=frozen)
            public = io.StringIO()
            with patch.object(runner.forward_acquire, '_plan', return_value={
                    'items': [item('2026-10-14', 0)], 'max_bytes': 1024**3}), \
                 patch.object(runner.forward_acquire, 'request') as download, \
                 patch.object(runner.subprocess, 'run') as child, contextlib.redirect_stdout(public):
                self.assertEqual(runner.run(args), 2)
            child.assert_not_called()
            download.assert_not_called()
            status = json.loads((output / 'status.json').read_text())
            self.assertEqual(status['failed_stage'], 'acquisition')
            self.assertFalse(status['strategy_evaluated'])
            self.assertNotIn('2026-10', public.getvalue())
            self.assertNotIn('AWAITING', public.getvalue())
            payload = json.loads(research_vault.unseal(next(output.glob('*.vault')).read_bytes(), self.private))
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload['payload_base64'])), mode='r:gz') as archive:
                self.assertIn('AWAITING_FROZEN_EPISODE_SOURCE_DATES', archive.extractfile('diagnostics.txt').read().decode())
                metadata = json.load(archive.extractfile('run.json'))
                self.assertEqual(metadata['requested_utc_days'], ['2026-10-15', '2026-10-16'])
                self.assertEqual(metadata['budgets']['max_snapshot_archives'], 192)
                self.assertTrue(metadata['partial_coverage'])
                self.assertNotIn('results/research.json', archive.getnames())

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
