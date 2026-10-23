"""Bounded public-data research with recipient-verified sealed delivery only.

No wallet, private identity, order client, or live execution path is imported.
Plaintext stays in a temporary directory; public output is an allowlisted status
and X25519 envelopes compatible with research_vault.py.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'analysis'), str(ROOT / 'tools')]
import forward_acquire
import research_vault

DAY_NAME = re.compile(r'^snapshots-(\d{4}-\d{2}-\d{2})-\d{6}\.jsonl\.gz$')
CACHE_NAME = re.compile(r'^[0-9a-f]{64}\.blob$')
PART_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def spread_order(items):
    """Visit chronological midpoints, then their largest remaining intervals."""
    result, intervals = [], [(0, len(items))]
    while intervals:
        intervals.sort(key=lambda pair: (-(pair[1] - pair[0]), pair[0]))
        lo, hi = intervals.pop(0)
        if lo >= hi:
            continue
        mid = (lo + hi) // 2
        result.append(items[mid])
        intervals.extend([(lo, mid), (mid + 1, hi)])
    return result


def record_unselected_source_issues(plan):
    """Keep unusable non-candidates in provenance without failing selected data.

    This is only for source metadata defects found before selection. Errors on
    selected items, including transport/integrity failures, remain mandatory.
    """
    excluded = plan.setdefault('excluded_source_issues', [])
    for item in plan['items']:
        if item.get('pre_error') and not item.get('selected'):
            excluded.append({'tag': item['tag'], 'name': item['name'],
                             'bytes': item.get('bytes'), 'source_issue': item['pre_error'],
                             'selected': False})
            item['selection_exclusion_reason'] = 'source_metadata_unusable_before_selection'
            item['pre_error'] = None
    return plan


def frozen_episode_bounds(frozen, initial_cash):
    """Reuse the frozen protocol's validation before narrowing acquisition."""
    if frozen.get('kind') != 'research_challenger':
        return None
    import quality_first_research as research
    payload = dict(frozen)
    expected = payload.pop('sha256', None)
    if research.digest(payload) != expected or payload.get('version') != research.VERSION:
        raise ValueError('FROZEN_MODEL_INTEGRITY_MISMATCH')
    if payload.get('protocol_sha256') != research.digest(research.protocol_contract()):
        raise ValueError('FROZEN_MODEL_PROTOCOL_CHANGED')
    if initial_cash != payload.get('initial_cash'):
        raise ValueError('FROZEN_INITIAL_CASH_CHANGED')
    return research.validate_research_challenger(payload)


def frozen_episode_utc_days(bounds):
    begin, end = bounds
    if (type(begin) is not int or type(end) is not int or begin < 0
            or end - begin != 24 * 60 * 60 * 1000):
        raise ValueError('INVALID_FROZEN_EPISODE_BOUNDS')
    # Subtract before conversion: a midnight end does not include the next day.
    return sorted({datetime.fromtimestamp(value // 1000, timezone.utc).date().isoformat()
                   for value in (begin, end - 1)})


def stratify_plan(plan, max_snapshot_archives=96, episode_bounds=None):
    """Reserve each source day, or each frozen episode day, before download.

    Complete snapshot shards are units. Nearby same-release/day label shards
    are co-sampled when present; their identity/timing is not certified here.
    Unequal or incomplete days remain visible in the manifest;
    this does not fill missing history or assert prospective observations.
    Frozen date scope includes a quota for missing days and never falls back.
    """
    if not 8 <= max_snapshot_archives <= 512:
        raise ValueError('INVALID_SNAPSHOT_ARCHIVE_LIMIT')
    by_day, labels = {}, {}
    release_dates = {r['tag']: str(r.get('published_at', '')) for r in plan.get('releases', [])}
    for item in plan['items']:
        item['selected'] = False
        if item['name'].startswith('labels-') and not item.get('pre_error'):
            match = DAY_NAME.fullmatch(item['name'].replace('labels-', 'snapshots-', 1))
            if match and type(item.get('bytes')) is int and item['bytes'] > 0:
                labels.setdefault((item['tag'], match[1]), []).append(item)
    for item in plan['items']:
        match = DAY_NAME.fullmatch(item['name'])
        if match and not item.get('pre_error') and type(item.get('bytes')) is int and item['bytes'] > 0:
            unit = [item]
            nearby = labels.get((item['tag'], match[1]), [])
            if nearby:
                sequence = int(item['name'].rsplit('-', 1)[1].split('.')[0])
                label = min(nearby, key=lambda other: abs(int(other['name'].rsplit('-', 1)[1].split('.')[0]) - sequence))
                unit.append(label)
            by_day.setdefault(match[1], []).append(unit)
    source_days = sorted(by_day)
    scope = {}
    if episode_bounds is not None:
        days = frozen_episode_utc_days(episode_bounds)
        by_day = {day: units for day, units in by_day.items() if day in days}
        scope = {
            'selection': 'frozen_episode_utc_days',
            'observation_scope': 'frozen_episode_utc_day_archives',
            'forward_min_window_start_ms': episode_bounds[0],
            'forward_max_window_end_ms': episode_bounds[1],
            'requested_utc_days': days,
            'eligible_source_utc_days': source_days,
            'missing_utc_days': sorted(set(days) - set(by_day)),
            'partial_coverage': True,
            'coverage_basis': 'archive_filename_utc_dates_only; not_complete_episode_observations',
        }
        for item in plan['items']:
            match = DAY_NAME.fullmatch(item['name'].replace('labels-', 'snapshots-', 1))
            if match and match[1] not in days:
                item['selection_exclusion_reason'] = 'outside_frozen_episode_utc_days'
        if not by_day:
            raise ValueError('AWAITING_FROZEN_EPISODE_SOURCE_DATES: requested=' + ','.join(days)
                             + '; eligible_source=' + ','.join(source_days))
    else:
        if not by_day:
            raise ValueError('NO_DATED_SNAPSHOT_SHARDS')
        days = source_days
    queues = {day: spread_order(sorted(by_day.get(day, []), key=lambda unit: (release_dates.get(unit[0]['tag'], ''), unit[0]['tag'], unit[0]['name']))) for day in days}
    quota = plan['max_bytes'] // len(days)
    used = dict.fromkeys(days, 0)
    selected = dict.fromkeys(days, 0)
    remaining = max_snapshot_archives
    while remaining and any(queues.values()):
        for day in days:
            if not remaining:
                break
            while queues[day]:
                unit = queues[day].pop(0)
                size = sum(item['bytes'] for item in unit if not item['selected'])
                if used[day] + size > quota:
                    continue
                for item in unit:
                    item['selected'] = True
                used[day] += size
                selected[day] += 1
                remaining -= 1
                break
    if not any(selected.values()):
        raise ValueError('NO_COMPLETE_SHARD_WITHIN_DAY_QUOTA')
    plan['reserved_bytes'] = sum(used.values())
    plan['research_selection'] = {
        'version': 1,
        'method': 'utc_day_equal_byte_quota_round_robin_midpoint_spread_complete_shards',
        'observation_scope': 'retrospective_recent_public_archives',
        'prospective_evidence': False,
        'max_snapshot_archives': max_snapshot_archives,
        'available_utc_days': sorted(by_day),
        'per_day_byte_quota': quota,
        'selected_snapshot_archives_by_day': selected,
        'selected_bytes_by_day': used,
        'label_selection': 'same_release_and_day_nearest_segment_sequence; not causal_label_certification',
        'all_history_complete': False,
        **scope,
    }
    return record_unselected_source_issues(plan)


@contextmanager
def acquisition_adapters(cache_dir, max_snapshot_archives, episode_bounds=None):
    """Reuse only locally rehashed bytes that match a freshly fetched sidecar."""
    original_plan, original_request = forward_acquire._plan, forward_acquire.request
    expected = {}
    cache = Path(cache_dir) if cache_dir is not None else None
    if cache is not None:
        if cache.is_symlink():
            raise ValueError('CACHE_SYMLINK')
        cache.mkdir(parents=True, exist_ok=True)

    def plan(*args, **kwargs):
        return stratify_plan(original_plan(*args, **kwargs), max_snapshot_archives, episode_bounds)

    def request(url, **kwargs):
        archive = url.endswith('.jsonl.gz')
        if archive and cache is not None and url in expected:
            target = cache / (hashlib.sha256(url.encode()).hexdigest() + '.blob')
            if target.is_file() and not target.is_symlink() and target.stat().st_size <= forward_acquire.RESPONSE_CAP:
                body = target.read_bytes()
                if hashlib.sha256(body).hexdigest() == expected[url]:
                    return body
        body = original_request(url, **kwargs)
        if url.endswith('.jsonl.gz.sha256'):
            expected[url[:-7]] = forward_acquire._checksum(body, url[:-7].rsplit('/', 1)[-1])
        elif archive and cache is not None and url in expected and hashlib.sha256(body).hexdigest() == expected[url]:
            target = cache / (hashlib.sha256(url.encode()).hexdigest() + '.blob')
            temporary = target.with_suffix('.part')
            if target.is_symlink() or temporary.is_symlink():
                raise ValueError('CACHE_SYMLINK')
            temporary.write_bytes(body)
            os.replace(temporary, target)
        return body

    forward_acquire._plan, forward_acquire.request = plan, request
    try:
        yield
    finally:
        forward_acquire._plan, forward_acquire.request = original_plan, original_request


def trim_cache(cache_dir, max_bytes):
    if cache_dir is None:
        return
    items = [p for p in Path(cache_dir).glob('*.blob') if CACHE_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()]
    total = sum(p.stat().st_size for p in items)
    for path in sorted(items, key=lambda p: p.stat().st_mtime):
        if total <= max_bytes:
            break
        size = path.stat().st_size
        path.unlink()
        total -= size


def load_recipient(encoded, expected_id):
    if not encoded or len(encoded) > 8192 or not re.fullmatch(r'[0-9a-f]{64}', expected_id or ''):
        raise ValueError('RECIPIENT_REQUIRED')
    recipient = research_vault.load_json_bytes(base64.b64decode(encoded, validate=True), 4096)
    research_vault.validate_recipient(recipient, expected_id)
    # Reject invalid/low-order X25519 points before acquisition or research.
    research_vault.inspect_envelope(research_vault.seal(b'identity-probe', recipient, expected_id))
    return recipient


def _bundle_stat(metadata):
    return tuple(getattr(metadata, key) for key in ('st_dev', 'st_ino', 'st_mode', 'st_size',
        'st_mtime_ns', 'st_ctime_ns', 'st_nlink'))


def _bundle_open(root_fd, relative):
    """Resolve every directory through non-following descriptors, not a raced path."""
    descriptor = os.dup(root_fd)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return os.open(relative.parts[-1], flags, dir_fd=descriptor)
    except OSError as exc:
        raise ValueError('RESULT_CHANGED') from exc
    finally:
        os.close(descriptor)


class _BundleBuffer(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_BUNDLE_BYTES:
            raise ValueError('BUNDLE_SIZE_LIMIT')
        return super().write(value)


def make_bundle(private_dir, *, max_member_bytes=MAX_BUNDLE_BYTES, max_expanded_bytes=MAX_EXPANDED_BYTES):
    """Bundle allowlisted evidence with separately bounded source and gzip sizes.

    Existing callers retain the 128 MiB member limit. An explicit opt-in permits
    members up to 256 MiB; source files and the full expanded tar (including
    metadata and record padding) remain at most 512 MiB. Gzip remains at most
    128 MiB. Source-file totals are checked before compression.
    """
    if (type(max_member_bytes) is not int or not 0 < max_member_bytes <= MAX_MEMBER_BYTES
            or type(max_expanded_bytes) is not int or not 0 < max_expanded_bytes <= MAX_EXPANDED_BYTES):
        raise ValueError('BUNDLE_BUDGET')
    private_dir = Path(private_dir)
    if private_dir.is_symlink():
        raise ValueError('RESULT_SYMLINK')
    root_fd = os.open(private_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
    members = [private_dir / name for name in ('run.json', 'frozen_input.json', 'diagnostics.txt', 'analysis-output.txt', 'results')]
    members += [private_dir / 'acquisition' / name for name in ('acquisition_manifest.json', 'acquisition_plan.json')]
    planned, directories, expanded = [], {}, 0
    try:
        for member in members:
            if member.is_symlink():
                raise ValueError('RESULT_SYMLINK')
            if not member.exists():
                continue
            paths = [member, *sorted(member.rglob('*'))] if member.is_dir() else [member]
            for path in paths:
                if path.is_symlink():
                    raise ValueError('RESULT_SYMLINK')
                relative = path.relative_to(private_dir)
                descriptor = _bundle_open(root_fd, relative)
                try:
                    metadata = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                if stat.S_ISDIR(metadata.st_mode):
                    directories[relative] = _bundle_stat(metadata)
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError('RESULT_NOT_REGULAR')
                if metadata.st_size > max_member_bytes:
                    raise ValueError('RESULT_SIZE_LIMIT')
                expanded += metadata.st_size
                if expanded > max_expanded_bytes:
                    raise ValueError('BUNDLE_EXPANDED_SIZE_LIMIT')
                planned.append((relative, _bundle_stat(metadata)))
        buffer = _BundleBuffer()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            def check_tar_expanded_size():
                # TarFile.offset includes PAX headers and padded file contents.
                # close() appends two zero blocks and pads to RECORDSIZE.
                final_size = ((archive.offset + 2 * tarfile.BLOCKSIZE + tarfile.RECORDSIZE - 1)
                    // tarfile.RECORDSIZE) * tarfile.RECORDSIZE
                if final_size > max_expanded_bytes:
                    raise ValueError('BUNDLE_EXPANDED_SIZE_LIMIT')
            check_tar_expanded_size()
            for relative, expected in planned:
                descriptor = _bundle_open(root_fd, relative)
                with os.fdopen(descriptor, 'rb') as handle:
                    metadata = os.fstat(handle.fileno())
                    if _bundle_stat(metadata) != expected:
                        raise ValueError('RESULT_CHANGED')
                    info = tarfile.TarInfo(str(relative))
                    info.size, info.mode, info.mtime = metadata.st_size, stat.S_IMODE(metadata.st_mode), metadata.st_mtime
                    info.uid, info.gid = metadata.st_uid, metadata.st_gid
                    archive.addfile(info, handle)
                    check_tar_expanded_size()
                    if handle.tell() != metadata.st_size or _bundle_stat(os.fstat(handle.fileno())) != expected:
                        raise ValueError('RESULT_CHANGED')
        for relative, expected in directories.items():
            descriptor = _bundle_open(root_fd, relative)
            try:
                if _bundle_stat(os.fstat(descriptor)) != expected:
                    raise ValueError('RESULT_CHANGED')
            finally:
                os.close(descriptor)
        return buffer.getvalue()
    finally:
        os.close(root_fd)


def deliver_bundle(bundle, output, recipient, expected_id):
    output = Path(output)
    chunks = [bundle[i:i + PART_BYTES] for i in range(0, len(bundle), PART_BYTES)] or [b'']
    digest = hashlib.sha256(bundle).hexdigest()
    for index, chunk in enumerate(chunks):
        payload = research_vault.canonical({
            'format': 'quality-first-research-tar-gzip-part-v1',
            'index': index, 'total': len(chunks), 'bundle_sha256': digest,
            'payload_base64': base64.b64encode(chunk).decode('ascii'),
        })
        envelope = research_vault.seal(payload, recipient, expected_id)
        research_vault.inspect_envelope(envelope)
        research_vault.write_new(output / f'part-{index:04d}.vault', envelope)
    return len(chunks)


def strategy_evaluated(result_dir):
    """Classify the analysis result, not the existence of output files.

    Unknown, missing or inconsistent metadata is a research failure. A valid
    waiting/empty episode can still be delivered successfully without claiming
    that a strategy was evaluated.
    """
    path = Path(result_dir) / 'research.json'
    if path.is_symlink():
        raise ValueError('RESEARCH_RESULT_SYMLINK')
    result = research_vault.load_json_bytes(
        research_vault.read_bounded(path, MAX_BUNDLE_BYTES), MAX_BUNDLE_BYTES)
    if (type(result.get('version')) is not int or result['version'] != 1
            or result.get('research_only') is not True or result.get('live_enabled') is not False):
        raise ValueError('RESEARCH_RESULT_SCHEMA')
    role = result.get('sample_role')
    replay_fields = {
        'retrospective_discovery_and_untuned_chronological_holdout': ('holdout', 'holdout_stress'),
        'prospective_after_frozen_boundary': ('forward', 'forward_stress'),
        'prospective_research_challenger': ('forward', 'forward_stress'),
    }.get(role)
    if replay_fields is None:
        raise ValueError('RESEARCH_RESULT_ROLE')
    funnel = result.get('quality_funnel')
    rows = funnel.get('input_rows') if isinstance(funnel, dict) else None
    if type(rows) is not int or rows < 0:
        raise ValueError('RESEARCH_RESULT_OBSERVATIONS')
    for field in replay_fields:
        replay = result.get(field)
        summary = replay.get('summary') if isinstance(replay, dict) else None
        attempts = summary.get('attempts') if isinstance(summary, dict) else None
        if type(attempts) is not int or attempts < 0 or (rows == 0 and attempts != 0):
            raise ValueError('RESEARCH_RESULT_REPLAY')
    if role == 'prospective_research_challenger':
        count = result.get('new_episode_observation_count')
        state = result.get('evaluation_status')
        if type(count) is not int or count != rows:
            raise ValueError('RESEARCH_RESULT_EPISODE_COUNT')
        if state in ('awaiting_new_data', 'no_observations_within_episode') and count == 0:
            return False
        if state == 'bounded_episode_replay' and count > 0:
            return True
        raise ValueError('RESEARCH_RESULT_EPISODE_STATUS')
    if 'evaluation_status' in result:
        raise ValueError('RESEARCH_RESULT_UNEXPECTED_STATUS')
    return rows > 0


def run(args):
    recipient = load_recipient(args.recipient_json_b64, args.recipient_id)
    output = Path(args.output)
    if output.exists():
        raise ValueError('DELIVERY_DIRECTORY_ALREADY_EXISTS')
    if not 16 <= args.max_releases <= 96 or not 64 * 1024**2 <= args.max_bytes <= 2 * 1024**3:
        raise ValueError('ACQUISITION_BUDGET')
    if not 0 < args.initial_cash <= 100 or not 1 <= args.timeout_seconds <= 3600:
        raise ValueError('RESEARCH_BUDGET')
    challenger = getattr(args, 'research_challenger', None)
    if challenger not in (None, 'spot_residual') or (challenger and getattr(args, 'frozen_model', None)):
        raise ValueError('RESEARCH_MODE_CONFLICT')
    output.mkdir(parents=True, mode=0o700)
    status = {'version': 1, 'status': 'failed', 'stage': 'acquisition', 'strategy_evaluated': False,
              'live_orders_enabled': False, 'plaintext_published': False}
    with tempfile.TemporaryDirectory(prefix='quality-first-private-') as temporary:
        private = Path(temporary)
        metadata = {
            'created_utc': datetime.now(timezone.utc).isoformat(),
            'source_commit': os.environ.get('GITHUB_SHA'),
            'run_id': os.environ.get('GITHUB_RUN_ID'),
            'observation_scope': 'retrospective_recent_public_archives',
            'prospective_evidence': False,
            'budgets': {'max_releases': args.max_releases, 'max_bytes': args.max_bytes,
                        'max_snapshot_archives': args.max_snapshot_archives, 'initial_cash': args.initial_cash,
                        'timeout_seconds': args.timeout_seconds},
            'source_sha256': {},
            'requested_research_challenger': challenger,
        }
        for path in [Path(__file__), ROOT / 'analysis/forward_acquire.py', ROOT / 'analysis/quality_first_research.py', ROOT / 'analysis/quality_first_data.py']:
            if path.is_file():
                metadata['source_sha256'][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (private / 'run.json').write_text(json.dumps(metadata, sort_keys=True, indent=2))
        with (private / 'diagnostics.txt').open('w') as diagnostics, redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            try:
                episode_bounds = None
                if getattr(args, 'frozen_model', None) is not None:
                    frozen_input = research_vault.read_bounded(args.frozen_model, 4 * 1024 * 1024)
                    frozen = research_vault.load_json_bytes(frozen_input, 4 * 1024 * 1024)
                    if not {'model', 'sha256', 'forward_min_window_start_ms'} <= set(frozen):
                        raise ValueError('FROZEN_MODEL_SCHEMA')
                    research_vault.write_new(private / 'frozen_input.json', frozen_input)
                    episode_bounds = frozen_episode_bounds(frozen, args.initial_cash)
                    if episode_bounds is not None:
                        metadata.update(selection='frozen_episode_utc_days',
                                        observation_scope='frozen_episode_utc_day_archives',
                                        forward_min_window_start_ms=episode_bounds[0],
                                        forward_max_window_end_ms=episode_bounds[1],
                                        requested_utc_days=frozen_episode_utc_days(episode_bounds),
                                        partial_coverage=True)
                        (private / 'run.json').write_text(json.dumps(metadata, sort_keys=True, indent=2))
                with acquisition_adapters(args.cache, args.max_snapshot_archives, episode_bounds):
                    manifest = forward_acquire.acquire(private / 'acquisition', args.max_releases, args.max_bytes)
                    if manifest.get('errors') and not manifest.get('acquisition_halted') and any(e.get('retryable') for e in manifest['errors']):
                        manifest = forward_acquire.acquire(private / 'acquisition', args.max_releases, args.max_bytes, resume=True)
                if not forward_acquire.validate_manifest(manifest):
                    raise ValueError('INCOMPLETE_SELECTED_ACQUISITION')
                status['stage'] = 'research'
                command = [
                    sys.executable, str(ROOT / 'analysis/quality_first_research.py'),
                    '--input', str(private / 'acquisition'), '--output', str(private / 'results'),
                    '--initial-cash', str(args.initial_cash),
                ]
                if getattr(args, 'frozen_model', None) is not None:
                    command += ['--frozen-model', str(private / 'frozen_input.json')]
                if challenger:
                    command += ['--research-challenger', challenger]
                with (private / 'analysis-output.txt').open('w') as log:
                    completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout_seconds, check=False,
                       env={'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1'})
                if completed.returncode:
                    raise ValueError('RESEARCH_PROCESS_FAILED')
                if not (private / 'results').is_dir() or not any((private / 'results').iterdir()):
                    raise ValueError('RESEARCH_OUTPUT_MISSING')
                status['strategy_evaluated'] = strategy_evaluated(private / 'results')
                status['status'] = 'complete'
            except Exception as exc:
                # Diagnostics are sealed below; exception text is never public.
                print(type(exc).__name__, str(exc))
            finally:
                trim_cache(args.cache, 2 * args.max_bytes)
        status['sealed_parts'] = deliver_bundle(make_bundle(private), output, recipient, args.recipient_id)
    if status['status'] != 'complete':
        status['failed_stage'] = status['stage']
    status['stage'] = 'sealed_delivery'
    (output / 'status.json').write_text(json.dumps(status, sort_keys=True, indent=2))
    print(json.dumps(status, sort_keys=True))
    return 0 if status['status'] == 'complete' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipient-json-b64', default=os.environ.get('RESEARCH_RECIPIENT_JSON_B64'))
    parser.add_argument('--recipient-id', default=os.environ.get('RESEARCH_RECIPIENT_ID'))
    parser.add_argument('--output', default='sealed-delivery')
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--max-releases', type=int, default=64)
    parser.add_argument('--max-bytes', type=int, default=1024**3)
    parser.add_argument('--max-snapshot-archives', type=int, default=96)
    parser.add_argument('--initial-cash', type=float, default=10)
    parser.add_argument('--frozen-model', type=Path, help='Existing local frozen model; copied only into the temporary sealed work bundle')
    parser.add_argument('--research-challenger', choices=['spot_residual'], help='Export a fixed research-only challenger; does not promote a strategy')
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args)
    except Exception:
        print('{"version":1,"status":"failed","stage":"delivery_contract","plaintext_published":false}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
