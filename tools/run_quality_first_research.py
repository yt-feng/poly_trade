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


def stratify_plan(plan, max_snapshot_archives=96):
    """Reserve a byte quota for every available UTC day before any download.

    Complete snapshot shards are units. Nearby same-release/day label shards
    are co-sampled when present; their identity/timing is not certified here.
    Unequal or incomplete days remain visible in the manifest;
    this does not fill missing history or assert prospective observations.
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
    if not by_day:
        raise ValueError('NO_DATED_SNAPSHOT_SHARDS')
    days = sorted(by_day)
    queues = {day: spread_order(sorted(by_day[day], key=lambda unit: (release_dates.get(unit[0]['tag'], ''), unit[0]['tag'], unit[0]['name']))) for day in days}
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
        'available_utc_days': days,
        'per_day_byte_quota': quota,
        'selected_snapshot_archives_by_day': selected,
        'selected_bytes_by_day': used,
        'label_selection': 'same_release_and_day_nearest_segment_sequence; not causal_label_certification',
        'all_history_complete': False,
    }
    return record_unselected_source_issues(plan)


@contextmanager
def acquisition_adapters(cache_dir, max_snapshot_archives):
    """Reuse only locally rehashed bytes that match a freshly fetched sidecar."""
    original_plan, original_request = forward_acquire._plan, forward_acquire.request
    expected = {}
    cache = Path(cache_dir) if cache_dir is not None else None
    if cache is not None:
        if cache.is_symlink():
            raise ValueError('CACHE_SYMLINK')
        cache.mkdir(parents=True, exist_ok=True)

    def plan(*args, **kwargs):
        return stratify_plan(original_plan(*args, **kwargs), max_snapshot_archives)

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


def make_bundle(private_dir):
    """Include results and provenance, never the raw tape/cache or identity."""
    private_dir = Path(private_dir)
    members = [private_dir / name for name in ('run.json', 'frozen_input.json', 'diagnostics.txt', 'analysis-output.txt', 'results')]
    members += [private_dir / 'acquisition' / name for name in ('acquisition_manifest.json', 'acquisition_plan.json')]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for member in members:
            if not member.exists():
                continue
            paths = sorted(member.rglob('*')) if member.is_dir() else [member]
            for path in paths:
                if path.is_symlink():
                    raise ValueError('RESULT_SYMLINK')
                if not path.is_file():
                    continue
                if path.stat().st_size > MAX_BUNDLE_BYTES:
                    raise ValueError('RESULT_SIZE_LIMIT')
                archive.add(path, arcname=str(path.relative_to(private_dir)), recursive=False)
                if buffer.tell() > MAX_BUNDLE_BYTES:
                    raise ValueError('BUNDLE_SIZE_LIMIT')
    value = buffer.getvalue()
    if len(value) > MAX_BUNDLE_BYTES:
        raise ValueError('BUNDLE_SIZE_LIMIT')
    return value


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


def run(args):
    recipient = load_recipient(args.recipient_json_b64, args.recipient_id)
    output = Path(args.output)
    if output.exists():
        raise ValueError('DELIVERY_DIRECTORY_ALREADY_EXISTS')
    if not 16 <= args.max_releases <= 96 or not 64 * 1024**2 <= args.max_bytes <= 2 * 1024**3:
        raise ValueError('ACQUISITION_BUDGET')
    if not 0 < args.initial_cash <= 100 or not 1 <= args.timeout_seconds <= 3600:
        raise ValueError('RESEARCH_BUDGET')
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
        }
        for path in [Path(__file__), ROOT / 'analysis/forward_acquire.py', ROOT / 'analysis/quality_first_research.py', ROOT / 'analysis/quality_first_data.py']:
            if path.is_file():
                metadata['source_sha256'][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (private / 'run.json').write_text(json.dumps(metadata, sort_keys=True, indent=2))
        with (private / 'diagnostics.txt').open('w') as diagnostics, redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            try:
                if getattr(args, 'frozen_model', None) is not None:
                    frozen_input = research_vault.read_bounded(args.frozen_model, 4 * 1024 * 1024)
                    frozen = research_vault.load_json_bytes(frozen_input, 4 * 1024 * 1024)
                    if not {'model', 'sha256', 'forward_min_window_start_ms'} <= set(frozen):
                        raise ValueError('FROZEN_MODEL_SCHEMA')
                    research_vault.write_new(private / 'frozen_input.json', frozen_input)
                with acquisition_adapters(args.cache, args.max_snapshot_archives):
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
                with (private / 'analysis-output.txt').open('w') as log:
                    completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout_seconds, check=False,
                       env={'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1'})
                if completed.returncode:
                    raise ValueError('RESEARCH_PROCESS_FAILED')
                if not (private / 'results').is_dir() or not any((private / 'results').iterdir()):
                    raise ValueError('RESEARCH_OUTPUT_MISSING')
                status['status'] = 'complete'
                status['strategy_evaluated'] = True
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
