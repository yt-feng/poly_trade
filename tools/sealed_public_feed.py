"""Bounded, public GET-only evidence collection, sealed before publication.

No account authentication, orders, strategy, wallet ranking, or PnL inference.
A trusted recipient is mandatory BEFORE the first network request. Filters,
raw replies, timing and errors stay inside the encrypted envelope. A CLI run
prints only a constant status; it never falls back to plaintext or another API.
This is a client contract, not validation of the remote service's completeness.
Reference: https://data-api.polymarket.com/v2/docs
"""
from __future__ import annotations
import argparse
import base64
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request
from urllib.error import HTTPError, URLError
from research_vault import canonical, load_json_bytes, read_bounded, seal, validate_recipient, write_new

HOST = 'https://data-api.polymarket.com'
ENDPOINTS = {'/v2/trades', '/v2/activity'}
MAX_BODY = 1024 * 1024
MAX_TOTAL = 6 * 1024 * 1024
MAX_PAGES = 5
FIELDS = {'user', 'condition', 'event_id', 'start', 'end', 'side', 'filter_type', 'filter_amount', 'taker_only', 'limit', 'sort_direction'}


def require(value, code):
    if not value:
        raise ValueError(code)


def validate_request(request):
    require(isinstance(request, dict) and set(request) == {'endpoint', 'filters', 'max_pages'}, 'INVALID_REQUEST')
    endpoint, filters, pages = request['endpoint'], request['filters'], request['max_pages']
    require(endpoint in ENDPOINTS, 'ENDPOINT_NOT_ALLOWED')
    require(isinstance(filters, dict) and set(filters) <= FIELDS, 'UNKNOWN_FILTER')
    require(type(pages) is int and 1 <= pages <= MAX_PAGES, 'INVALID_PAGE_BUDGET')
    for k, v in filters.items():
        require(type(v) in (str, int, float, bool) and len(str(v)) <= 4096, 'INVALID_FILTER_VALUE')
        if isinstance(v, float):
            require(Decimal(str(v)).is_finite(), 'INVALID_FILTER_VALUE')
    if endpoint == '/v2/activity':
        require(bool(filters.get('user')), 'ACTIVITY_REQUIRES_USER')
    if endpoint == '/v2/trades' and not filters.get('user'):
        require('start' not in filters and 'end' not in filters, 'TIME_FILTER_IGNORED_FOR_NONUSER_SHAPE')
    for k in ('start', 'end'):
        if k in filters:
            require(type(filters[k]) is int and filters[k] > 0, 'EXPLICIT_POSITIVE_TIME_REQUIRED')
    if 'start' in filters and 'end' in filters:
        require(filters['start'] <= filters['end'], 'REVERSED_TIME_RANGE')
    if 'limit' in filters:
        require(type(filters['limit']) is int and 1 <= filters['limit'] <= 1000, 'INVALID_PAGE_SIZE')
    return endpoint, dict(filters), pages


def next_query(filters, cursor):
    # Feed cursors do not bind filters: resend the identical complete filter set.
    result = dict(filters)
    if cursor is not None:
        require(isinstance(cursor, str) and 0 < len(cursor) <= 8192, 'INVALID_CURSOR')
        result['cursor'] = cursor
    return result


def parse_page(body):
    require(isinstance(body, bytes) and len(body) <= MAX_BODY, 'BODY_LIMIT')
    doc = load_json_bytes(body, MAX_BODY)
    require(isinstance(doc.get('data'), list), 'INVALID_DATA_ENVELOPE')
    require(all(isinstance(x, dict) for x in doc['data']), 'INVALID_ROW')
    paging = doc.get('pagination')
    require(isinstance(paging, dict), 'PAGINATION_REQUIRED')
    require(type(paging.get('has_more')) is bool and 'next_cursor' in paging, 'INVALID_PAGINATION')
    more, cursor = paging['has_more'], paging['next_cursor']
    if more:
        require(isinstance(cursor, str) and 0 < len(cursor) <= 8192 and len(doc['data']) > 0, 'INCONSISTENT_PAGINATION')
    else:
        require(cursor is None, 'INCONSISTENT_PAGINATION')
    return doc['data'], cursor, more


def market_units(size, price):
    """Explicit arithmetic only: a share count is not dollar volume or PnL."""
    if size is None or price is None:
        return {'shares': None, 'gross_notional_usdc': None, 'pnl_known': False}
    require(type(size) is not bool and type(price) is not bool, 'INVALID_UNITS')
    try:
        q, p = Decimal(str(size)), Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('INVALID_UNITS') from None
    require(q.is_finite() and p.is_finite() and q >= 0 and 0 <= p <= 1, 'INVALID_UNITS')
    return {'shares': str(q), 'gross_notional_usdc': str(q * p), 'pnl_known': False}


def profile_counts(row):
    require(isinstance(row, dict), 'INVALID_PROFILE')
    # The profile card's `trades` counts markets, not fills. Never substitute.
    return {'distinct_markets_traded': row.get('trades'),
            'fill_count': (row.get('all_time_pnl') or {}).get('trade_count'),
            'actual_roi': None}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('REDIRECT_REFUSED')


def public_get(endpoint, filters):
    require(endpoint in ENDPOINTS, 'ENDPOINT_NOT_ALLOWED')
    query = {k: str(v).lower() if type(v) is bool else v for k, v in filters.items()}
    opener = build_opener(ProxyHandler({}), NoRedirect())
    url = HOST + endpoint + '?' + urlencode(query)
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'public-evidence-client/1'}, method='GET')
    try:
        with opener.open(req, timeout=8) as response:
            body = response.read(MAX_BODY + 1)
            return response.status, body
    except HTTPError as exc:
        # Do not retain error pages or follow access/rate refusals with fallbacks.
        return exc.code, b''
    except (URLError, TimeoutError, OSError):
        raise ValueError('TRANSPORT_UNAVAILABLE') from None


def collect(request, recipient, fingerprint, fetcher=public_get, clock=None):
    validate_recipient(recipient, fingerprint)
    endpoint, filters, max_pages = validate_request(request)
    now = clock or (lambda: time.time_ns() // 1000000)
    record = {'schema': 1, 'scope': 'public_observation_only', 'request': request,
              'observed_pages': [], 'completion': 'BUDGET_STOP', 'all_history_complete': False,
              'remote_truth_authenticated': False, 'orders_enabled': False}
    cursor, seen, total = None, set(), 0
    for _ in range(max_pages):
        started = now()
        try:
            status, body = fetcher(endpoint, next_query(filters, cursor))
            received = now()
            require(received >= started, 'CLOCK_REVERSED')
            require(type(status) is int, 'INVALID_HTTP_STATUS')
            if status != 200:
                record['completion'] = 'HTTP_STOP'
                record['http_status'] = status
                break
            require(isinstance(body, bytes) and len(body) <= MAX_BODY, 'BODY_LIMIT')
            require(total + len(body) <= MAX_TOTAL, 'TOTAL_BODY_LIMIT')
            total += len(body)
            record['observed_pages'].append({'request_started_ms': started, 'received_ms': received,
                'response_sha256': hashlib.sha256(body).hexdigest(),
                'raw_utf8_b64': base64.b64encode(body).decode('ascii')})
            rows, next_cursor, more = parse_page(body)
            if not more:
                record['completion'] = 'CURSOR_EXHAUSTED_FOR_REQUEST'
                break
            require(next_cursor not in seen, 'CURSOR_LOOP')
            seen.add(next_cursor)
            cursor = next_cursor
        except Exception:
            # Keep no remote exception messages: they may embed filters/addresses.
            record['completion'] = 'VALIDATION_OR_TRANSPORT_STOP'
            break
    record['finished_ms'] = now()
    # No caller receives plaintext records from this API.
    return seal(canonical(record), recipient, fingerprint)


def main():
    parser = argparse.ArgumentParser(description='Public feed to encrypted evidence only; no trading.')
    parser.add_argument('--request', required=True, help='Private local JSON path, not inline wallet parameters')
    parser.add_argument('--recipient', default='research_vault/recipient.json')
    parser.add_argument('--recipient-id', required=True, help='Previously independently checked public fingerprint')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        # Check output and recipient before reading a query or making any request.
        out = Path(args.output)
        require(out.suffix == '.vault' and not out.exists(), 'NEW_VAULT_PATH_REQUIRED')
        recipient = load_json_bytes(read_bounded(args.recipient, 4096), 4096)
        validate_recipient(recipient, args.recipient_id)
        query_path = Path(args.request).expanduser().resolve()
        cwd = Path.cwd().resolve()
        require(query_path != cwd and cwd not in query_path.parents, 'PRIVATE_QUERY_OUTSIDE_REPOSITORY')
        request = load_json_bytes(read_bounded(query_path, 65536), 65536)
        encrypted = collect(request, recipient, args.recipient_id)
        write_new(out, encrypted, 0o600)
        print('SEALED_EVIDENCE_WRITTEN. Completion details require decryption; this is not a data-completeness or strategy certificate.')
    except Exception:
        print('SEALED_COLLECTION_BLOCKED. No plaintext fallback or access bypass was attempted.')
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
