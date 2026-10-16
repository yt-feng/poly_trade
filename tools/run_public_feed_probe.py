"""Fixed public-feed availability evidence, collected only in GitHub Actions.

All HTTP bodies, clocks and exceptions are inside the sealed delivery. A
completed delivery does not assess feed freshness, completeness or a strategy.
No recipient private key, account credential, wallet filter or order is used.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import research_vault
import run_quality_first_research as delivery
import sealed_public_feed

HOST = 'https://data-api.polymarket.com'
STATUS_ENDPOINT = '/v2/status'
TRADES_ENDPOINT = '/v2/trades'
BODY_LIMIT = 1024 * 1024
MAX_REQUESTS = 4
REQUEST_TIMEOUT_SECONDS = 8
FIXED_REQUEST = {'endpoint': TRADES_ENDPOINT,
                 'filters': {'limit': 100, 'taker_only': True}, 'max_pages': 2}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('REDIRECT_REFUSED')


def validate_http_request(endpoint, filters):
    if endpoint == STATUS_ENDPOINT and filters == {}:
        return
    if (endpoint != TRADES_ENDPOINT or not isinstance(filters, dict)
            or set(filters) not in ({'limit', 'taker_only'}, {'limit', 'taker_only', 'cursor'})
            or type(filters.get('limit')) is not int or filters['limit'] != 100
            or filters.get('taker_only') is not True):
        raise ValueError('FIXED_PUBLIC_REQUEST_REQUIRED')
    if 'cursor' in filters and (not isinstance(filters['cursor'], str) or not 0 < len(filters['cursor']) <= 8192):
        raise ValueError('FIXED_PUBLIC_REQUEST_REQUIRED')


def public_get(endpoint, filters):
    """Bounded standard urllib transport; keep normal environment and TLS."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('GITHUB_ACTIONS_REQUIRED')
    validate_http_request(endpoint, filters)
    query = {key: str(value).lower() if type(value) is bool else value for key, value in filters.items()}
    url = HOST + endpoint + ('?' + urlencode(query) if query else '')
    request = Request(url, headers={'Accept': 'application/json',
                                    'User-Agent': 'sealed-public-feed-probe/1'}, method='GET')
    # Default handlers preserve ordinary environment networking. Only redirects
    # are denied, so the fixed endpoint can never turn into a fallback request.
    opener = build_opener(NoRedirect())

    def deadline(signum, frame):
        raise TimeoutError('REQUEST_DEADLINE')

    # The cloud runner is Linux and calls this on the main thread. A wall-clock
    # deadline also bounds a slow trickle, beyond the socket inactivity timeout.
    previous_handler = signal.signal(signal.SIGALRM, deadline)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, REQUEST_TIMEOUT_SECONDS)
    try:
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.status, response.read(BODY_LIMIT + 1)
        except HTTPError as response:
            # Preserve bounded refusal/error bytes privately, without retries.
            with response:
                return response.code, response.read(BODY_LIMIT + 1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] or previous_timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def exception_code(exc):
    """A finite diagnostic vocabulary; never persist remote exception text."""
    if isinstance(exc, TimeoutError):
        return 'TIMEOUT'
    if isinstance(exc, URLError):
        return 'TRANSPORT_UNAVAILABLE'
    if isinstance(exc, OSError):
        return 'IO_UNAVAILABLE'
    if isinstance(exc, ValueError):
        return 'REQUEST_OR_RESPONSE_CONTRACT'
    return 'CLIENT_EXCEPTION'


def utc(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


class HTTPRecorder:
    """Record every attempted status/feed request only for the outer vault."""
    def __init__(self, fetcher=None, clock=None, monotonic=None):
        self.fetcher = fetcher or public_get
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.monotonic = monotonic or time.monotonic_ns
        self.records = []

    def __call__(self, endpoint, filters):
        validate_http_request(endpoint, filters)
        if len(self.records) >= MAX_REQUESTS:
            raise ValueError('REQUEST_BUDGET_EXCEEDED')
        started, monotonic_start = self.clock(), self.monotonic()
        record = {'endpoint': endpoint, 'query': dict(filters),
                  'request_started_ms': started, 'request_started_utc': utc(started),
                  'received_ms': None, 'received_utc': None, 'http_status': None,
                  'raw_body_base64': None, 'retained_body_sha256': None,
                  'exception_code': None}
        self.records.append(record)
        try:
            status, body = self.fetcher(endpoint, filters)
            received = self.clock()
            record.update(received_ms=received, received_utc=utc(received))
            if type(status) is not int or not 100 <= status <= 599 or not isinstance(body, bytes):
                raise ValueError('INVALID_HTTP_RESPONSE')
            record['http_status'] = status
            retained = body[:BODY_LIMIT]
            record.update(raw_body_base64=base64.b64encode(retained).decode('ascii'),
                          retained_body_sha256=hashlib.sha256(retained).hexdigest(),
                          body_bytes_read=len(body), body_bytes_retained=len(retained),
                          body_exceeded_cap=len(body) > BODY_LIMIT)
            if len(body) > BODY_LIMIT:
                raise ValueError('BODY_LIMIT')
            return status, body
        except Exception as exc:
            record['exception_code'] = exception_code(exc)
            # collect catches this; status calls also continue to the next phase.
            raise ValueError('SEALED_HTTP_OBSERVATION_FAILED') from None
        finally:
            finished = self.clock()
            record.update(finished_ms=finished, finished_utc=utc(finished),
                          monotonic_elapsed_ns=max(0, self.monotonic() - monotonic_start))


def observe_status(recorder):
    try:
        recorder(STATUS_ENDPOINT, {})
    except Exception:
        # The recorder retained an explicit missing-receipt/exception state.
        pass


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
    with tempfile.TemporaryDirectory(prefix='sealed-feed-probe-private-') as temporary:
        private = Path(temporary)
        results = private / 'results'
        results.mkdir()
        metadata = {'version': 1, 'scope': 'public_feed_availability_evidence_only',
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'source_commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
                    'status_schema_assessed': False, 'freshness_assessed': False,
                    'all_history_complete': False, 'strategy_evaluated': False,
                    'receipt_semantics': 'bounded_body_read_returned; not_exchange_event_time',
                    'budgets': {'max_requests': MAX_REQUESTS, 'body_bytes_per_request': BODY_LIMIT,
                                'request_timeout_seconds': REQUEST_TIMEOUT_SECONDS},
                    'request': FIXED_REQUEST, 'collection_exception_code': None,
                    'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (Path(__file__), Path(sealed_public_feed.__file__),
                                                   Path(research_vault.__file__), Path(delivery.__file__))}}
        recorder = HTTPRecorder()
        with (private / 'diagnostics.txt').open('w') as diagnostics, redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            observe_status(recorder)
            try:
                request = json.loads(json.dumps(FIXED_REQUEST))
                envelope = sealed_public_feed.collect(request, recipient, recipient_id, fetcher=recorder)
                inspected = research_vault.inspect_envelope(envelope)
                if inspected['recipient_id'] != recipient_id:
                    raise ValueError('INNER_RECIPIENT_MISMATCH')
                research_vault.write_new(results / 'public-feed.vault', envelope)
            except Exception as exc:
                metadata['collection_exception_code'] = exception_code(exc)
                status.update(status='failed', failed_stage='acquisition')
            finally:
                observe_status(recorder)
        (private / 'run.json').write_bytes(research_vault.canonical(metadata))
        (results / 'http-observations.json').write_bytes(research_vault.canonical({
            'version': 1, 'records': recorder.records, 'status_schema_assessed': False,
            'freshness_assessed': False, 'all_history_complete': False, 'strategy_evaluated': False}))
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
        # No HTTP/body/timing/exception/recipient details in public logs.
        print('SEALED_FEED_PROBE_BLOCKED; PLAINTEXT_PUBLISHED=false; STRATEGY_EVALUATED=false')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
