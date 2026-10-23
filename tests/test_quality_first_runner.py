"""Synthetic tests: no public feed, personal identity, wallet or order access."""
import argparse
import base64
import contextlib
import copy
import hashlib
import gzip
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
import persist_quality_first_journal as journal
from test_quality_first_research import raw_row, synthetic_provenance
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

    def run_with_result(self, result):
        """Exercise delivery with synthetic child bytes; never acquire data."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'delivery'
            args = argparse.Namespace(recipient_json_b64=base64.b64encode(research_vault.canonical(self.recipient)).decode(),
                recipient_id=self.rid, output=output, cache=None, max_releases=64, max_bytes=1024**3,
                max_snapshot_archives=96, initial_cash=10, timeout_seconds=10)
            def child(command, **kwargs):
                destination = Path(command[command.index('--output') + 1])
                destination.mkdir()
                if result is None:
                    (destination / 'unrelated.txt').write_text('nonempty is not evidence')
                else:
                    (destination / 'research.json').write_bytes(result)
                return argparse.Namespace(returncode=0)
            with patch.object(runner.forward_acquire, 'acquire', return_value={'errors': []}), \
                 patch.object(runner.forward_acquire, 'validate_manifest', return_value=True), \
                 patch.object(runner.forward_acquire, 'request', side_effect=AssertionError('network forbidden')) as network, \
                 patch.object(runner.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(io.StringIO()):
                code = runner.run(args)
            network.assert_not_called()
            status = json.loads((output / 'status.json').read_text())
            self.assertEqual(journal.validate_status(status), status)
            bundle = journal.validate_delivery(output, self.rid)
            self.assertEqual(json.loads(bundle['status.json']), status)
            payload = json.loads(research_vault.unseal(bundle['part-0000.vault'], self.private))
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload['payload_base64'])), mode='r:gz') as archive:
                if result is not None:
                    self.assertEqual(archive.extractfile('results/research.json').read(), result)
            return code, status

    def challenger_result(self, rows):
        challenger = research.research_challenger_payload(
            {'family': 'spot_residual', 'coef': [.20] + [0.] * 6, 'fit_cutoff_ms': 1_000_000},
            'synthetic', 4_000_000, 10., 5_000_000)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, 'load_verified_snapshots', return_value=(rows, synthetic_provenance(rows or [raw_row(0, 0)]))):
            root = Path(directory)
            frozen = root / 'frozen.json'
            frozen.write_text(json.dumps(challenger))
            research.run(root / 'input', root / 'output', frozen_model=frozen)
            return (root / 'output/research.json').read_bytes()

    def test_actual_waiting_empty_and_bounded_metadata_controls_evaluation_and_journal(self):
        begin, end = 5_100_000, 91_500_000
        cases = [([], 'awaiting_new_data', False),
                 ([raw_row(0, 0)], 'awaiting_new_data', False),
                 ([raw_row(end, 0)], 'no_observations_within_episode', False),
                 ([raw_row(begin, 0)], 'bounded_episode_replay', True)]
        for rows, evaluation_status, evaluated in cases:
            with self.subTest(status=evaluation_status, count=len(rows)):
                result = self.challenger_result(rows)
                self.assertEqual(json.loads(result)['evaluation_status'], evaluation_status)
                code, status = self.run_with_result(result)
                self.assertEqual(code, 0)
                self.assertEqual(status['status'], 'complete')
                self.assertIs(status['strategy_evaluated'], evaluated)
                self.assertNotIn('failed_stage', status)

    def test_actual_discovery_and_legacy_frozen_results_remain_supported(self):
        rows = [raw_row(i * 300_000, 0) for i in range(10)]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, 'load_verified_snapshots', return_value=(rows, synthetic_provenance(rows))):
            root = Path(directory)
            research.run(root / 'input', root / 'discovery')
            frozen_path = root / 'discovery/frozen_model.json'
            begin = json.loads(frozen_path.read_text())['forward_min_window_start_ms']
            forward_rows = [raw_row(begin, offset) for offset in range(0, 300_000, 1000)]
            with patch.object(research, 'load_verified_snapshots',
                              return_value=(forward_rows, synthetic_provenance(forward_rows))):
                research.run(root / 'input', root / 'forward', frozen_model=frozen_path)
            for name in ('discovery', 'forward'):
                code, status = self.run_with_result((root / name / 'research.json').read_bytes())
                self.assertEqual(code, 0)
                self.assertTrue(status['strategy_evaluated'])

    def test_unknown_damaged_or_inconsistent_metadata_fails_closed(self):
        good = json.loads(self.challenger_result([raw_row(5_100_000, 0)]))
        cases = [None, b'{', b'[]', b'null', b'{}', b'\xff', b'{"version":1,"version":1}']
        mutations = [
            {'version': 2}, {'version': True}, {'sample_role': 'unknown'},
            {'research_only': False}, {'live_enabled': True},
            {'evaluation_status': 'unknown'}, {'evaluation_status': None},
            {'evaluation_status': 'awaiting_new_data'}, {'evaluation_status': 'no_observations_within_episode'},
            {'new_episode_observation_count': 0}, {'new_episode_observation_count': True},
            {'new_episode_observation_count': '1'}, {'quality_funnel': {}},
            {'quality_funnel': {'input_rows': True}},
            {'quality_funnel': {'input_rows': -1}}, {'forward': None},
            {'forward_stress': {'summary': {}}},
            {'forward': {'summary': {'attempts': True}}},
        ]
        for update in mutations:
            result = copy.deepcopy(good)
            result.update(update)
            cases.append(research.canonical(result))
        for field in ('sample_role', 'evaluation_status', 'new_episode_observation_count', 'forward_stress'):
            result = copy.deepcopy(good)
            del result[field]
            cases.append(research.canonical(result))
        empty = json.loads(self.challenger_result([]))
        empty['evaluation_status'] = 'bounded_episode_replay'
        cases.append(research.canonical(empty))
        for index, result in enumerate(cases):
            with self.subTest(index=index):
                code, status = self.run_with_result(result)
                self.assertEqual(code, 2)
                self.assertEqual(status['status'], 'failed')
                self.assertEqual(status['failed_stage'], 'research')
                self.assertFalse(status['strategy_evaluated'])

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

    def test_bundle_member_opt_in_preserves_legacy_default_without_large_compression(self):
        self.assertEqual(runner.make_bundle.__kwdefaults__["max_member_bytes"], 128 * 1024**2)
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            target = private / "run.json"
            with target.open("wb") as handle:
                handle.truncate(128 * 1024**2 + 1)  # Sparse metadata only: rejected before compression.
            with patch.object(runner.tarfile, "open", side_effect=AssertionError("must precheck")):
                with self.assertRaisesRegex(ValueError, "RESULT_SIZE_LIMIT"):
                    runner.make_bundle(private)
            target.write_bytes(b"x" * 96)
            with patch.dict(runner.make_bundle.__kwdefaults__, {"max_member_bytes": 64, "max_expanded_bytes": 16384}), \
                    patch.object(runner, "MAX_MEMBER_BYTES", 128), patch.object(runner, "MAX_EXPANDED_BYTES", 16384), \
                    patch.object(runner, "MAX_BUNDLE_BYTES", 4096):
                with self.assertRaisesRegex(ValueError, "RESULT_SIZE_LIMIT"):
                    runner.make_bundle(private)
                value = runner.make_bundle(private, max_member_bytes=128)
            with tarfile.open(fileobj=io.BytesIO(value), mode="r:gz") as archive:
                self.assertEqual(archive.extractfile("run.json").read(), b"x" * 96)

    def test_bundle_budget_types_and_hard_bounds_reject_before_source_read(self):
        invalid = [dict(max_member_bytes=value) for value in (True, False, 0, -1, 1.5, runner.MAX_MEMBER_BYTES + 1)]
        invalid += [dict(max_expanded_bytes=value) for value in (True, 0, -1, 1.5, runner.MAX_EXPANDED_BYTES + 1)]
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "_bundle_open", side_effect=AssertionError("no source read")):
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "BUNDLE_BUDGET"):
                    runner.make_bundle(Path(directory), **kwargs)

    def test_bundle_aggregate_precheck_and_compressed_budget_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            (private / "run.json").write_bytes(b"a" * 64)
            (private / "diagnostics.txt").write_bytes(b"b" * 64)
            with patch.object(runner.tarfile, "open", side_effect=AssertionError("must precheck")):
                with self.assertRaisesRegex(ValueError, "BUNDLE_EXPANDED_SIZE_LIMIT"):
                    runner.make_bundle(private, max_member_bytes=64, max_expanded_bytes=127)
            with patch.object(runner, "MAX_BUNDLE_BYTES", 32):
                with self.assertRaisesRegex(ValueError, "BUNDLE_SIZE_LIMIT"):
                    runner.make_bundle(private)

    def test_bundle_expanded_tar_budget_includes_pax_headers_and_record_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            target = private / "run.json"; target.write_bytes(b"x")
            runner.os.utime(target, ns=(1_700_000_000_123456789, 1_700_000_000_123456789))
            value = runner.make_bundle(private, max_expanded_bytes=tarfile.RECORDSIZE)
            self.assertEqual(len(gzip.decompress(value)), tarfile.RECORDSIZE)
            with self.assertRaisesRegex(ValueError, "BUNDLE_EXPANDED_SIZE_LIMIT"):
                runner.make_bundle(private, max_expanded_bytes=tarfile.RECORDSIZE - 1)
            results = private / "results"; results.mkdir()
            for index in range(6):
                path = results / (str(index) + ".json"); path.write_bytes(b"x")
                runner.os.utime(path, ns=(1_700_000_000_123456789, 1_700_000_000_123456789))
            with self.assertRaisesRegex(ValueError, "BUNDLE_EXPANDED_SIZE_LIMIT"):
                runner.make_bundle(private, max_expanded_bytes=tarfile.RECORDSIZE)
            full = runner.make_bundle(private, max_expanded_bytes=2 * tarfile.RECORDSIZE)
            self.assertEqual(len(gzip.decompress(full)), 2 * tarfile.RECORDSIZE)

    def test_bundle_symlink_and_nonregular_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, outside = root / "private", root / "outside"
            private.mkdir(); outside.mkdir(); (outside / "data").write_bytes(b"outside")
            (private / "run.json").symlink_to(outside / "data")
            with self.assertRaisesRegex(ValueError, "RESULT_SYMLINK"):
                runner.make_bundle(private)
            (private / "run.json").unlink()
            (private / "results").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "RESULT_SYMLINK"):
                runner.make_bundle(private)
            (private / "results").unlink()
            runner.os.mkfifo(private / "run.json")
            with self.assertRaisesRegex(ValueError, "RESULT_NOT_REGULAR"):
                runner.make_bundle(private)

    def test_bundle_rejects_source_changes_during_read(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            target = private / "run.json"; target.write_bytes(b"AAAA")
            original = runner.tarfile.TarFile.addfile
            def changed(archive, info, handle):
                target.write_bytes(b"BBBB")
                return original(archive, info, handle)
            with patch.object(runner.tarfile.TarFile, "addfile", autospec=True, side_effect=changed):
                with self.assertRaisesRegex(ValueError, "RESULT_CHANGED"):
                    runner.make_bundle(private)

    def test_bundle_rejects_replacement_between_precheck_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            target = private / "run.json"; target.write_bytes(b"AAAA")
            original = runner._bundle_open; count = 0
            def changed(root_fd, relative):
                nonlocal count
                count += 1
                if count == 2:
                    target.unlink(); target.write_bytes(b"AAAA")
                return original(root_fd, relative)
            with patch.object(runner, "_bundle_open", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "RESULT_CHANGED"):
                    runner.make_bundle(private)

    def test_bundle_parent_directory_swap_cannot_follow_outside_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir()
            results = private / "results"; results.mkdir(); (results / "data").write_bytes(b"inside")
            outside = root / "outside"; outside.mkdir(); (outside / "data").write_bytes(b"outside")
            original = runner._bundle_open; file_opens = 0
            def changed(root_fd, relative):
                nonlocal file_opens
                if str(relative) == "results/data":
                    file_opens += 1
                    if file_opens == 2:
                        results.rename(private / "original-results")
                        results.symlink_to(outside, target_is_directory=True)
                return original(root_fd, relative)
            with patch.object(runner, "_bundle_open", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "RESULT_CHANGED"):
                    runner.make_bundle(private)

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
                (result / 'research.json').write_bytes(self.challenger_result([raw_row(5_100_000, 0)]))
                return argparse.Namespace(returncode=0)
            with patch.object(runner.forward_acquire, 'acquire', return_value={'errors': []}), \
                 patch.object(runner.forward_acquire, 'validate_manifest', return_value=True), \
                 patch.object(runner.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.run(args), 0)


if __name__ == '__main__':
    unittest.main()
