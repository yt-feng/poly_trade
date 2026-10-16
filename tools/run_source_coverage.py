"""Cloud-only audit of a privately registered, immutable public-archive sample.

No archive selection, training, strategy evaluation or orders take place here.
Raw archives remain temporary; only provenance and audit results are sealed.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

import research_vault
import run_quality_first_research as delivery

acquisition = delivery.forward_acquire
REPOSITORY = 'yt-feng/poly'
API = 'https://api.github.com/repos/' + REPOSITORY
DOWNLOAD = 'https://github.com/' + REPOSITORY + '/releases/download/'
MAX_BYTES = 32 * 1024**2
MAX_INFLATED_BYTES = 512 * 1024**2
MAX_LINE_BYTES = 1024**2
MAX_ROWS = 100_000
MAX_REQUEST_BYTES = 32 * 1024
MAX_METADATA_BYTES = 2 * 1024**2
MAX_ASSET_PAGES = 10
REQUEST_TIMEOUT = 30
ARCHIVE_RE = re.compile(r'(snapshots|labels)-(\d{4}-\d{2}-\d{2})-\d{6}\.jsonl\.gz')
DIGEST_RE = re.compile(r'sha256:[0-9a-f]{64}')


def audit_module():
    import settlement_source_coverage
    return settlement_source_coverage


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('NONFINITE_JSON')
        return value

    def invalid_constant(value):
        raise ValueError('NONFINITE_JSON')

    return json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                      parse_float=finite_float, parse_constant=invalid_constant)


def positive_int(value):
    return type(value) is int and value > 0


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('TIMESTAMP_REQUIRED')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('UTC_OFFSET_REQUIRED')
    return value


def decode_request(encoded):
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_REQUEST_BYTES * 2:
        raise ValueError('REGISTERED_REQUEST_REQUIRED')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError('REGISTERED_REQUEST_LIMIT')
    request = strict_json(raw)
    keys = {'version', 'source_repository', 'source_run_id', 'source_run_attempt',
            'source_head_sha', 'release_id', 'release_tag', 'max_bytes', 'selected_assets',
            'registered_utc', 'selection_rule', 'audit_policy'}
    if not isinstance(request, dict) or set(request) != keys or type(request['version']) is not int or request['version'] != 1:
        raise ValueError('REGISTERED_REQUEST_SCHEMA')
    if (request['source_repository'] != REPOSITORY
            or not isinstance(request['source_run_id'], str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', request['source_run_id'])
            or not positive_int(request['source_run_attempt'])
            or not positive_int(request['release_id'])
            or not isinstance(request['source_head_sha'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', request['source_head_sha'])
            or request['release_tag'] != f"capture-v2-{request['source_run_id']}-{request['source_run_attempt']}"):
        raise ValueError('REGISTERED_SOURCE_IDENTITY')
    if type(request['max_bytes']) is not int or request['max_bytes'] != MAX_BYTES:
        raise ValueError('REGISTERED_BYTE_BUDGET')
    timestamp(request['registered_utc'])
    if not isinstance(request['selection_rule'], str) or not 1 <= len(request['selection_rule']) <= 2048:
        raise ValueError('SELECTION_PROVENANCE_REQUIRED')
    audit_module().validate_policy(request['audit_policy'])
    assets = request['selected_assets']
    if not isinstance(assets, list) or not 2 <= len(assets) <= 8:
        raise ValueError('REGISTERED_ASSET_COUNT')
    names, ids, counts, total = set(), set(), {'snapshots': 0, 'labels': 0}, 0
    asset_keys = {'name', 'asset_id', 'bytes', 'api_digest', 'created_at',
                  'sidecar_asset_id', 'sidecar_bytes', 'sidecar_api_digest'}
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != asset_keys:
            raise ValueError('REGISTERED_ASSET_SCHEMA')
        match = ARCHIVE_RE.fullmatch(asset['name']) if isinstance(asset['name'], str) else None
        if not match:
            raise ValueError('REGISTERED_ARCHIVE_NAME')
        datetime.strptime(match[2], '%Y-%m-%d')
        if asset['name'] in names:
            raise ValueError('DUPLICATE_REGISTERED_ASSET')
        names.add(asset['name']); counts[match[1]] += 1
        for key in ('asset_id', 'sidecar_asset_id'):
            if not positive_int(asset[key]) or asset[key] in ids:
                raise ValueError('REGISTERED_ASSET_ID')
            ids.add(asset[key])
        if (not positive_int(asset['bytes']) or asset['bytes'] > MAX_BYTES
                or not positive_int(asset['sidecar_bytes']) or asset['sidecar_bytes'] > 4096):
            raise ValueError('REGISTERED_ASSET_SIZE')
        for key in ('api_digest', 'sidecar_api_digest'):
            if not isinstance(asset[key], str) or not DIGEST_RE.fullmatch(asset[key]):
                raise ValueError('REGISTERED_ASSET_DIGEST')
        timestamp(asset['created_at'])
        total += asset['bytes'] + asset['sidecar_bytes']
    if not all(1 <= count <= 4 for count in counts.values()) or total > MAX_BYTES:
        raise ValueError('REGISTERED_SAMPLE_BUDGET')
    return request, raw


def pinned_plan(request):
    items = []
    for asset in request['selected_assets']:
        url = DOWNLOAD + request['release_tag'] + '/' + asset['name']
        items.append({'tag': request['release_tag'], 'name': asset['name'],
                      'bytes': asset['bytes'], 'asset_id': asset['asset_id'],
                      'created_at': asset['created_at'], 'url': url, 'side_url': url + '.sha256',
                      'api_digest': asset['api_digest'], 'selected': True, 'pre_error': None})
    return {'version': 1, 'source_repository': REPOSITORY, 'max_releases': 1,
            'max_bytes': MAX_BYTES, 'items': items,
            'reserved_bytes': sum(item['bytes'] for item in items),
            'selection_scope': 'registered_assets_only_not_full_release',
            'releases': [{'id': request['release_id'], 'tag': request['release_tag'],
                          'published_at': None, 'asset_count': len(items) * 2,
                          'eligible_snapshot_label_archives': len(items),
                          'count_scope': 'registered_assets_only_not_full_release'}]}


class SafeRedirect(acquisition.SafeRedirect):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError('UNEXPECTED_REDIRECT_IDENTITY')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PinnedTransport:
    """Closed GET routes, ordinary environment networking, cumulative body cap."""
    def __init__(self, request):
        self.routes = {
            API + '/actions/runs/' + request['source_run_id']: (MAX_METADATA_BYTES, None, True),
            API + '/releases/' + str(request['release_id']): (MAX_METADATA_BYTES, None, True)}
        for page in range(1, MAX_ASSET_PAGES + 1):
            self.routes[API + f"/releases/{request['release_id']}/assets?per_page=100&page={page}"] = (MAX_METADATA_BYTES, None, True)
        for asset in request['selected_assets']:
            url = DOWNLOAD + request['release_tag'] + '/' + asset['name']
            self.routes[url] = (asset['bytes'], asset['api_digest'], False)
            self.routes[url + '.sha256'] = (asset['sidecar_bytes'], asset['sidecar_api_digest'], False)
        self.bytes_read = 0
        self.records = []
        self.max_attempts = 2 * len(self.routes)

    def __call__(self, url, *, json_response=False, request_budget=None):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise ValueError('GITHUB_ACTIONS_REQUIRED')
        if url not in self.routes or request_budget is not None or json_response is not self.routes[url][2]:
            raise ValueError('UNREGISTERED_SOURCE_REQUEST')
        for attempt in range(2):
            try:
                body = self._once(url)
                return strict_json(body) if json_response else body
            except acquisition.TransportError as exc:
                if not exc.retryable or attempt == 1:
                    raise
                time.sleep(1)
        raise AssertionError('unreachable')

    def _once(self, url):
        if len(self.records) >= self.max_attempts or self.bytes_read >= MAX_BYTES:
            raise ValueError('HTTP_TOTAL_BUDGET')
        cap, digest, _ = self.routes[url]
        record = {'url': url, 'started_ms': time.time_ns() // 1_000_000,
                  'received_ms': None, 'http_status': None, 'bytes_read': 0,
                  'response_sha256': None, 'exception_code': None}
        self.records.append(record)
        mono = time.monotonic_ns()
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'sealed-source-coverage/1'}
        if url.startswith(API + '/') and os.environ.get('GH_TOKEN'):
            headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
        opener = build_opener(SafeRedirect())

        def deadline(signum, frame):
            raise TimeoutError('HTTP_DEADLINE')

        previous_handler = signal.signal(signal.SIGALRM, deadline)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, REQUEST_TIMEOUT)
        try:
            try:
                response = opener.open(Request(url, headers=headers, method='GET'), timeout=REQUEST_TIMEOUT)
            except HTTPError as exc:
                response = exc
            with response:
                record['http_status'] = response.status
                # A small sidecar's size bound applies to its successful body,
                # not to an error page. Preserve refusal and retry semantics.
                if response.status in acquisition.REFUSALS:
                    raise acquisition.AccessRefused(response.status, False)
                if response.status != 200:
                    raise acquisition.TransportError(response.status, 500 <= response.status <= 599)
                length = response.headers.get('Content-Length')
                if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) > cap):
                    raise ValueError('HTTP_RESPONSE_SIZE')
                chunks, size = [], 0
                while True:
                    remaining = MAX_BYTES - self.bytes_read
                    if remaining <= 0:
                        raise ValueError('HTTP_TOTAL_BUDGET')
                    chunk = response.read(min(64 * 1024, cap + 1 - size, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk); size += len(chunk)
                    self.bytes_read += len(chunk); record['bytes_read'] += len(chunk)
                    if size > cap:
                        raise ValueError('HTTP_RESPONSE_SIZE')
                body = b''.join(chunks)
                record.update(received_ms=time.time_ns() // 1_000_000,
                              response_sha256=hashlib.sha256(body).hexdigest())
                if length is not None and size != int(length):
                    raise acquisition.ResponseError('HTTP_RESPONSE_LENGTH')
                if digest and (size != cap or 'sha256:' + hashlib.sha256(body).hexdigest() != digest):
                    raise ValueError('REGISTERED_BODY_MISMATCH')
                return body
        except (TimeoutError, URLError, OSError):
            record['exception_code'] = 'TRANSPORT_UNAVAILABLE'
            raise acquisition.TransportError(None, True) from None
        except acquisition.TransportError:
            record['exception_code'] = 'HTTP_RESPONSE_UNAVAILABLE'
            raise
        except Exception:
            record['exception_code'] = 'SOURCE_CONTRACT'
            raise
        finally:
            record.update(finished_ms=time.time_ns() // 1_000_000,
                          monotonic_elapsed_ns=max(0, time.monotonic_ns() - mono))
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] or previous_timer[1]:
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def verify_source(request, transport, evidence):
    run = transport(API + '/actions/runs/' + request['source_run_id'], json_response=True)
    evidence['run'] = run
    if (not isinstance(run, dict) or type(run.get('id')) is not int
            or run['id'] != int(request['source_run_id'])
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != request['source_run_attempt']
            or run.get('head_sha') != request['source_head_sha']
            or not isinstance(run.get('repository'), dict)
            or run['repository'].get('full_name') != REPOSITORY):
        raise ValueError('SOURCE_RUN_MISMATCH')
    # A still-running capture may already have immutable, published shards.
    release = transport(API + '/releases/' + str(request['release_id']), json_response=True)
    evidence['release'] = release
    if (not isinstance(release, dict) or type(release.get('id')) is not int
            or release['id'] != request['release_id'] or release.get('tag_name') != request['release_tag']):
        raise ValueError('SOURCE_RELEASE_MISMATCH')
    wanted = {}
    for asset in request['selected_assets']:
        wanted[asset['asset_id']] = (asset['name'], asset['bytes'], asset['api_digest'], asset['created_at'])
        wanted[asset['sidecar_asset_id']] = (asset['name'] + '.sha256', asset['sidecar_bytes'], asset['sidecar_api_digest'], None)

    def inspect_assets(assets):
        if not isinstance(assets, list):
            raise ValueError('SOURCE_ASSET_SCHEMA')
        found = set()
        for asset in assets:
            if not isinstance(asset, dict) or not positive_int(asset.get('id')):
                raise ValueError('SOURCE_ASSET_ID')
            if asset['id'] not in wanted:
                continue
            if asset['id'] in found:
                raise ValueError('SOURCE_ASSET_REPEATED')
            name, size, digest, created = wanted[asset['id']]
            if (asset.get('name') != name or type(asset.get('size')) is not int or asset['size'] != size
                    or asset.get('digest') != digest or (created is not None and asset.get('created_at') != created)):
                raise ValueError('SOURCE_ASSET_MISMATCH')
            found.add(asset['id'])
        return found

    found = inspect_assets(release.get('assets', []))
    if found != set(wanted):
        found = set(); evidence['asset_pages'] = []
        for page in range(1, MAX_ASSET_PAGES + 1):
            assets = transport(API + f"/releases/{request['release_id']}/assets?per_page=100&page={page}", json_response=True)
            evidence['asset_pages'].append(assets)
            current = inspect_assets(assets)
            if len(assets) > 100 or current & found:
                raise ValueError('SOURCE_ASSET_PAGINATION')
            found.update(current)
            if found == set(wanted) or len(assets) < 100:
                break
    if found != set(wanted):
        raise ValueError('REGISTERED_ASSET_NOT_FOUND')
    evidence['registered_identity_verified'] = True
    evidence['all_release_assets_enumerated'] = False


@contextmanager
def acquisition_adapter(request, transport):
    original_plan, original_request = acquisition._plan, acquisition.request
    plan = pinned_plan(request)

    def fixed_plan(max_releases, max_bytes):
        if max_releases != 1 or max_bytes != MAX_BYTES:
            raise ValueError('PINNED_PLAN_LIMITS_CHANGED')
        return plan

    acquisition._plan, acquisition.request = fixed_plan, transport
    try:
        yield
    finally:
        acquisition._plan, acquisition.request = original_plan, original_request


def load_records(root, manifest, stats):
    snapshots, labels = [], []
    stats.update(inflated_bytes=0, rows=0, files=[])
    for item in manifest['files']:
        path = root / item['path']
        if root.resolve() not in path.resolve().parents or path.is_symlink():
            raise ValueError('ARCHIVE_PATH')
        match = ARCHIVE_RE.fullmatch(item['name'])
        if not match or path.name != item['name']:
            raise ValueError('ARCHIVE_NAME')
        destination = snapshots if match[1] == 'snapshots' else labels
        file_stats = {'name': item['name'], 'rows': 0, 'inflated_bytes': 0, 'complete': False}
        stats['files'].append(file_stats)
        with path.open('rb') as compressed:
            compressed_size = os.fstat(compressed.fileno()).st_size
            digest = hashlib.file_digest(compressed, 'sha256').hexdigest()
            if compressed_size != item['bytes'] or digest != item['sha256']:
                raise ValueError('LOCAL_ARCHIVE_CHANGED')
            compressed.seek(0)
            with gzip.GzipFile(fileobj=compressed) as stream:
                while True:
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    stats['inflated_bytes'] += len(line); file_stats['inflated_bytes'] += len(line)
                    if len(line) > MAX_LINE_BYTES:
                        raise ValueError('INFLATED_LINE_LIMIT')
                    if stats['inflated_bytes'] > MAX_INFLATED_BYTES:
                        raise ValueError('INFLATED_TOTAL_LIMIT')
                    if stats['rows'] >= MAX_ROWS:
                        raise ValueError('ROW_LIMIT')
                    row = strict_json(line)
                    if not isinstance(row, dict):
                        raise ValueError('RECORD_OBJECT_REQUIRED')
                    if '__source__' in row:
                        raise ValueError('RESERVED_SOURCE_FIELD')
                    row['__source__'] = {'name': item['name'], 'row_index': file_stats['rows'] + 1,
                                         'sha256': item['sha256']}
                    destination.append(row)
                    stats['rows'] += 1; file_stats['rows'] += 1
        file_stats['complete'] = True
    return snapshots, labels


def run(args):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('GITHUB_ACTIONS_REQUIRED')
    recipient_id = os.environ.get('QUALITY_FIRST_RECIPIENT_ID')
    recipient = delivery.load_recipient(os.environ.get('QUALITY_FIRST_RECIPIENT_JSON_B64'), recipient_id)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValueError('DELIVERY_DIRECTORY_ALREADY_EXISTS')
    output.mkdir(parents=True, mode=0o700)
    status = {'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
              'strategy_evaluated': False, 'live_orders_enabled': False, 'plaintext_published': False}
    with tempfile.TemporaryDirectory(prefix='source-coverage-private-') as temporary:
        private = Path(temporary); results = private / 'results'; results.mkdir()
        metadata = {'version': 1, 'scope': 'registered_source_coverage_only',
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'source_commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
                    'strategy_evaluated': False, 'full_history_complete': False,
                    'raw_archives_in_delivery': False,
                    'budgets': {'compressed_archive_bytes': MAX_BYTES, 'http_response_body_bytes': MAX_BYTES,
                                'inflated_bytes': MAX_INFLATED_BYTES, 'line_bytes': MAX_LINE_BYTES, 'rows': MAX_ROWS},
                    'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (Path(__file__), Path(acquisition.__file__),
                                                   Path(delivery.__file__), Path(research_vault.__file__))}}
        evidence, stats, transport, stage = {}, {}, None, 'acquisition'
        with (private / 'diagnostics.txt').open('w') as diagnostics, redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            try:
                request, raw = decode_request(os.environ.get('SOURCE_COVERAGE_REQUEST_B64'))
                (results / 'source-request.json').write_bytes(raw)
                metadata['request_sha256'] = hashlib.sha256(raw).hexdigest()
                metadata['request_canonical_sha256'] = hashlib.sha256(research_vault.canonical(request)).hexdigest()
                metadata['producer_head_sha'] = request['source_head_sha']
                module = audit_module()
                metadata['source_sha256'][Path(module.__file__).name] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                transport = PinnedTransport(request)
                verify_source(request, transport, evidence)
                with acquisition_adapter(request, transport):
                    manifest = acquisition.acquire(private / 'acquisition', max_releases=1, max_bytes=MAX_BYTES)
                # These counters refer to registered assets, never the entire release.
                manifest.update(selection_scope='registered_assets_only_not_full_release', asset_pagination=False)
                acquisition.save_json(private / 'acquisition' / 'acquisition_manifest.json', manifest)
                if not acquisition.validate_manifest(manifest):
                    raise ValueError('REGISTERED_ACQUISITION_INCOMPLETE')
                stage = 'research'
                snapshots, labels = load_records(private / 'acquisition', manifest, stats)
                report = module.audit_records(snapshots, labels, request['audit_policy'])
                (results / 'coverage.json').write_bytes(research_vault.canonical(report))
            except Exception as exc:
                status.update(status='failed', failed_stage=stage)
                metadata['failure_type'] = type(exc).__name__
                # Error details are private, including malformed-source context.
                diagnostics.write(str(exc)[:1024] + '\n')
        evidence['http_records'] = transport.records if transport is not None else []
        evidence['http_body_bytes_read'] = transport.bytes_read if transport is not None else 0
        (results / 'source-verification.json').write_bytes(research_vault.canonical(evidence))
        (results / 'decode-statistics.json').write_bytes(research_vault.canonical(stats))
        (private / 'run.json').write_bytes(research_vault.canonical(metadata))
        status['sealed_parts'] = delivery.deliver_bundle(delivery.make_bundle(private), output, recipient, recipient_id)
    (output / 'status.json').write_bytes(research_vault.canonical(status))
    print(json.dumps(status, sort_keys=True))
    return 0 if status['status'] == 'complete' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='sealed-delivery')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args)
    except Exception:
        print('SEALED_SOURCE_COVERAGE_BLOCKED; PLAINTEXT_PUBLISHED=false; STRATEGY_EVALUATED=false')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
