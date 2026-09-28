"""Bounded, checksum-verified PUBLIC archive acquisition. No trading methods.

A selection plan is persisted before download. Recovery retains that plan and
rehashes successful files; it never silently substitutes a newer sample. A
failure remains a failure unless the missing bytes actually arrive and verify.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

API = 'https://api.github.com/repos/yt-feng/poly'
DEFAULT_MAX_BYTES = 450 * 1024 * 1024
RELEASE_RE = re.compile(r'capture-v2-\d+-\d+')
NAME_RE = re.compile(r'(?:snapshots|labels)-[0-9-]+\.jsonl\.gz')
RESPONSE_CAP = 64 * 1024 * 1024
REFUSALS = frozenset((401, 403, 418, 429, 451))


class TransportError(RuntimeError):
    def __init__(self, code=None, retryable=False, attempts=1):
        super().__init__('HTTP_TRANSPORT_FAILURE' if code else 'NETWORK_TRANSPORT_FAILURE')
        self.code, self.retryable, self.attempts = code, retryable, attempts


class AccessRefused(TransportError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow normal GitHub asset redirects, never forward credentials cross-host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname
        if urlparse(newurl).scheme != 'https' or host not in {
            'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
            'objects.githubusercontent.com', 'raw.githubusercontent.com'
        }:
            raise ValueError('UNEXPECTED_REDIRECT_HOST')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result is not None and host != urlparse(req.full_url).hostname:
            result.remove_header('Authorization')
        return result


def _retry_wait(headers, attempt):
    raw = headers.get('Retry-After') if headers else None
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(raw).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        # Do not sleep less than a valid server request or run indefinitely.
        if seconds > 60:
            raise TransportError(503, False)
        if seconds > 0:
            return seconds
    return min(8.0, 2.0 ** attempt) + random.uniform(0, 0.25)


def request(url: str) -> bytes:
    parsed = urlparse(url)
    valid = (url.startswith(API + '/') or
             url.startswith('https://github.com/yt-feng/poly/releases/download/'))
    if not valid or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('UNEXPECTED_PUBLIC_SOURCE')
    headers = {'User-Agent': 'poly-forward-readonly', 'Accept': 'application/vnd.github+json'}
    if parsed.hostname == 'api.github.com' and os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())
    for attempt in range(4):
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                length = resp.headers.get('Content-Length')
                if length and int(length) > RESPONSE_CAP:
                    raise ValueError('RESPONSE_BYTE_CAP')
                body = resp.read(RESPONSE_CAP + 1)
                if len(body) > RESPONSE_CAP:
                    raise ValueError('RESPONSE_BYTE_CAP')
                return body
        except urllib.error.HTTPError as exc:
            if exc.code in REFUSALS:
                raise AccessRefused(exc.code, False, attempt + 1) from None
            retryable = 500 <= exc.code <= 599
            if not retryable or attempt == 3:
                raise TransportError(exc.code, retryable, attempt + 1) from None
            delay = _retry_wait(exc.headers, attempt)
        except (TimeoutError, OSError) as exc:
            if attempt == 3:
                raise TransportError(None, True, attempt + 1) from None
            delay = _retry_wait(None, attempt)
        time.sleep(delay)
    raise AssertionError('unreachable')


def get_json(url):
    return json.loads(request(url))


def list_capture_releases(max_releases):
    releases = []
    for page in range(1, 6):
        batch = get_json(f'{API}/releases?per_page=100&page={page}')
        if not isinstance(batch, list):
            raise ValueError('RELEASE_SCHEMA')
        releases.extend(r for r in batch if RELEASE_RE.fullmatch(str(r.get('tag_name', ''))))
        if len(batch) < 100 or len(releases) >= max_releases:
            break
    releases.sort(key=lambda r: str(r.get('published_at', '')), reverse=True)
    return releases[:max_releases]


def list_release_assets(release_id):
    assets = []
    for page in range(1, 31):
        batch = get_json(f'{API}/releases/{release_id}/assets?per_page=100&page={page}')
        if not isinstance(batch, list):
            raise ValueError('ASSET_SCHEMA')
        assets.extend(batch)
        if len(batch) < 100:
            return assets
    raise ValueError('ASSET_PAGINATION_LIMIT')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('SYMLINK_OUTPUT')
    temp = path.with_name(path.name + '.part')
    with temp.open('wb') as handle:
        handle.write(value); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, path)


def _target(root, tag, name):
    if not RELEASE_RE.fullmatch(tag) or not NAME_RE.fullmatch(name):
        raise ValueError('UNSAFE_ARCHIVE_IDENTITY')
    p = root / 'captured' / tag / name
    if root.resolve() not in p.resolve().parents or p.is_symlink():
        raise ValueError('UNSAFE_ARCHIVE_PATH')
    return p


def _checksum(body, name):
    parts = body.decode('utf-8').strip().split()
    if not parts or not re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]):
        raise ValueError('INVALID_CHECKSUM_SIDECAR')
    if len(parts) > 2 or (len(parts) == 2 and parts[1].lstrip('*') != name):
        raise ValueError('CHECKSUM_FILENAME_MISMATCH')
    return parts[0].lower()


def _plan(max_releases, max_bytes):
    releases = list_capture_releases(max_releases)
    if not releases:
        raise ValueError('NO_CAPTURE_RELEASES')
    plan = {'version': 1, 'source_repository': 'yt-feng/poly', 'max_releases': max_releases,
            'max_bytes': max_bytes, 'releases': [], 'items': []}
    reserved = 0
    for release in releases:
        tag = str(release['tag_name']); rid = int(release['id'])
        if not RELEASE_RE.fullmatch(tag):
            raise ValueError('UNSAFE_RELEASE')
        assets = list_release_assets(rid)
        names = [str(a.get('name', '')) for a in assets]
        if len(names) != len(set(names)):
            raise ValueError('DUPLICATE_ASSET_NAMES')
        by_name = dict(zip(names, assets))
        eligible = [a for a in assets if str(a.get('name', '')).startswith(('snapshots-', 'labels-'))
                    and str(a.get('name', '')).endswith('.jsonl.gz')]
        plan['releases'].append({'id': rid, 'tag': tag, 'published_at': release.get('published_at'),
            'asset_count': len(assets), 'eligible_snapshot_label_archives': len(eligible)})
        for a in sorted(eligible, key=lambda x: x['name']):
            name = str(a['name']); side = by_name.get(name + '.sha256')
            if not NAME_RE.fullmatch(name):
                raise ValueError('UNSAFE_ARCHIVE_NAME')
            size = a.get('size')
            item = {'tag': tag, 'name': name, 'bytes': size, 'asset_id': a.get('id'),
                    'created_at': a.get('created_at'), 'url': a.get('browser_download_url'),
                    'side_url': side.get('browser_download_url') if side else None,
                    'api_digest': a.get('digest'), 'selected': False, 'pre_error': None}
            if side is None:
                item['pre_error'] = 'missing checksum sidecar'
            elif type(size) is not int or size < 0:
                item['pre_error'] = 'invalid asset size'
            elif reserved + size <= max_bytes:
                item['selected'] = True; reserved += size
            plan['items'].append(item)
    plan['reserved_bytes'] = reserved
    return plan


def _error(item, exc):
    code = getattr(exc, 'code', None)
    return {'tag': item['tag'], 'name': item['name'], 'error': str(exc)[:150],
            'error_type': type(exc).__name__, 'http_status': code,
            'retryable': bool(getattr(exc, 'retryable', False)),
            'access_refused': isinstance(exc, AccessRefused)}


def _manifest(plan, state, generation):
    m = {'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source_repository': 'yt-feng/poly', 'read_only': True, 'asset_pagination': True,
        'max_releases': plan['max_releases'], 'max_bytes': plan['max_bytes'],
        'downloaded_bytes': 0, 'eligible_archives_total': len(plan['items']),
        'budget_exhausted': False, 'releases': [], 'files': [], 'skipped': [], 'errors': [],
        'plan_sha256': digest(plan), 'recovery_generation': generation, 'failure_history': [],
        'acquisition_halted': False, 'full_history_complete': False,
        'resume_reuses_only_locally_rehashed_files': True}
    for r in plan['releases']:
        rec = dict(r, downloaded_snapshot_label_archives=0, budget_skipped_snapshot_label_archives=0)
        m['releases'].append(rec)
    for item in plan['items']:
        key = item['tag'] + '/' + item['name']; s = state.get(key, {})
        rr = next(r for r in m['releases'] if r['tag'] == item['tag'])
        m['failure_history'].extend(s.get('history', []))
        if item['pre_error']:
            m['errors'].append(_error(item, ValueError(item['pre_error'])))
        elif not item['selected']:
            m['budget_exhausted'] = True; rr['budget_skipped_snapshot_label_archives'] += 1
            m['skipped'].append({'tag': item['tag'], 'name': item['name'], 'bytes': item['bytes'], 'reason': 'download byte budget'})
        elif s.get('status') == 'verified':
            f = s['file']; m['files'].append(f); m['downloaded_bytes'] += f['bytes']
            rr['downloaded_snapshot_label_archives'] += 1
        else:
            m['errors'].append(s.get('error', _error(item, RuntimeError('NOT_YET_ACQUIRED'))))
    m['acquisition_halted'] = any(e.get('access_refused') for e in m['errors'])
    return m


def acquire(out: Path, max_releases=16, max_bytes=DEFAULT_MAX_BYTES, *, resume=False):
    if type(max_releases) is not int or max_releases <= 0 or type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('POSITIVE_INTEGER_LIMITS_REQUIRED')
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    planfile = out / 'acquisition_plan.json'; statefile = out / 'acquisition_state.json'
    if resume:
        envelope = json.loads(planfile.read_text()); plan = envelope['plan']
        if digest(plan) != envelope['sha256'] or plan['source_repository'] != 'yt-feng/poly':
            raise ValueError('PLAN_INTEGRITY_FAILURE')
        if plan['max_bytes'] != max_bytes or plan['max_releases'] != max_releases:
            raise ValueError('RESUME_LIMITS_CHANGED')
        saved = json.loads(statefile.read_text())
        if saved.get('plan_sha256') != digest(plan):
            raise ValueError('STATE_PLAN_MISMATCH')
        state, generation = saved['states'], saved['generation'] + 1
        if generation > 1:
            raise ValueError('RECOVERY_PASS_LIMIT')
    else:
        if planfile.exists() or statefile.exists():
            raise ValueError('EXISTING_PLAN_REQUIRES_EXPLICIT_RESUME')
        plan = _plan(max_releases, max_bytes); state = {}; generation = 0
        save_json(planfile, {'plan': plan, 'sha256': digest(plan)})
    def checkpoint():
        save_json(statefile, {'plan_sha256': digest(plan), 'generation': generation, 'states': state})
        result = _manifest(plan, state, generation)
        save_json(out / 'acquisition_manifest.json', result)
        return result
    checkpoint()
    halt = False
    for item in plan['items']:
        if item['pre_error'] or not item['selected']:
            continue
        key = item['tag'] + '/' + item['name']; s = state.setdefault(key, {'history': []})
        dest = _target(out, item['tag'], item['name'])
        sidefile = dest.with_name(dest.name + '.sha256')
        if s.get('status') == 'verified':
            try:
                if sidefile.is_symlink() or not dest.is_file() or not sidefile.is_file():
                    raise ValueError('LOCAL_CHECKPOINT_MISSING')
                body = dest.read_bytes(); expect = _checksum(sidefile.read_bytes(), item['name'])
                if len(body) != item['bytes'] or hashlib.sha256(body).hexdigest() != expect or expect != s['file']['sha256']:
                    raise ValueError('LOCAL_CHECKPOINT_CORRUPT')
            except Exception as exc:
                s.update(status='failed', error=_error(item, exc)); s['history'].append(dict(s['error'], generation=generation))
            continue
        if s.get('error', {}).get('access_refused'):
            halt = True
        if halt or (resume and s.get('status') == 'failed' and not s['error'].get('retryable')):
            continue
        try:
            side = request(item['side_url']); expected = _checksum(side, item['name'])
            ad = item.get('api_digest')
            if ad and ad != 'sha256:' + expected:
                raise ValueError('API_SIDECAR_DIGEST_CONFLICT')
            body = request(item['url'])
            if len(body) != item['bytes']:
                raise ValueError('DECLARED_SIZE_MISMATCH')
            if hashlib.sha256(body).hexdigest() != expected:
                raise ValueError('checksum mismatch')
            _write_bytes(dest, body); _write_bytes(sidefile, side)
            s.update(status='verified', error=None, file={
                'release': item['tag'], 'name': item['name'], 'path': str(dest.relative_to(out)),
                'bytes': len(body), 'sha256': expected, 'created_at': item['created_at'],
                'url': item['url'], 'asset_id': item['asset_id']})
        except Exception as exc:
            s.update(status='failed', error=_error(item, exc)); s['history'].append(dict(s['error'], generation=generation))
            halt = isinstance(exc, AccessRefused)
        checkpoint()
        if halt:
            break
    result = checkpoint()
    if not any(f['name'].startswith('snapshots-') for f in result['files']):
        result['no_verified_snapshots'] = True
        save_json(out / 'acquisition_manifest.json', result)
    return result


def validate_manifest(m):
    expected = sum(r['eligible_snapshot_label_archives'] for r in m.get('releases', []))
    return bool(expected > 0 and expected == len(m.get('files', [])) + len(m.get('skipped', [])) + len(m.get('errors', []))
                and not m.get('errors') and m.get('files')
                and any(f['name'].startswith('snapshots-') for f in m['files'])
                and m['downloaded_bytes'] == sum(f['bytes'] for f in m['files'])
                and m['downloaded_bytes'] <= m['max_bytes'])


def main():
    p = argparse.ArgumentParser(); p.add_argument('--output', default='forward_inputs')
    p.add_argument('--max-releases', type=int, default=16); p.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    p.add_argument('--resume', action='store_true'); p.add_argument('--recover-once', action='store_true')
    args = p.parse_args()
    r = acquire(Path(args.output), args.max_releases, args.max_bytes, resume=args.resume)
    if args.recover_once and not args.resume and r['errors'] and not r['acquisition_halted'] and any(e['retryable'] for e in r['errors']):
        time.sleep(15)
        r = acquire(Path(args.output), args.max_releases, args.max_bytes, resume=True)
    print(json.dumps({'files': len(r['files']), 'bytes': r['downloaded_bytes'],
        'skipped': len(r['skipped']), 'unresolved_errors': len(r['errors']),
        'failure_history_count': len(r['failure_history']), 'recovery_generation': r['recovery_generation'],
        'bounded_integrity_verified': validate_manifest(r), 'full_history_complete': False}, indent=2))
    if not validate_manifest(r):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
