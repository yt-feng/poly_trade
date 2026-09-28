"""Observe PUBLIC reference/constraint availability across an actual rollover.

Data acquisition only. No strategy signals, wallet, order, or cash-release methods.
Past first-availability is never inferred from a post-close response. CLOB itode
is observed directly instead of assuming all markets have the 250ms delay.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

SLUG = re.compile(r'btc-updown-5m-(\d{10})$')
CID = re.compile(r'0x[0-9a-fA-F]{64}$')
URL = re.compile(r'https://(?:gamma-api\.polymarket\.com/(?:markets|events)/slug/btc-updown-5m-\d{10}|clob\.polymarket\.com/(?:clob-markets/0x[0-9a-fA-F]{64}|book\?token_id=\d+))$')
BODY_CAP = 2 * 1024 * 1024
TOTAL_CAP = 32 * 1024 * 1024


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return str(n) if n.is_finite() and n > 0 else None


def array(value):
    return json.loads(value) if isinstance(value, str) else value


def identity(market, slug):
    if not isinstance(market, dict) or market.get('slug') != slug or not SLUG.fullmatch(slug):
        raise ValueError('MARKET_SLUG_MISMATCH')
    cid = str(market.get('conditionId', ''))
    if not CID.fullmatch(cid):
        raise ValueError('MARKET_CONDITION_INVALID')
    tokens, names = array(market.get('clobTokenIds', [])), array(market.get('outcomes', []))
    if not isinstance(tokens, list) or not isinstance(names, list) or len(tokens) != 2 or len(names) != 2:
        raise ValueError('MARKET_TOKENS_INVALID')
    if len(set(map(str, tokens))) != 2 or any(not str(t).isdigit() for t in tokens):
        raise ValueError('MARKET_TOKENS_INVALID')
    mapping = {str(n).lower(): str(t) for n, t in zip(names, tokens)}
    if set(mapping) != {'up', 'down'}:
        raise ValueError('MARKET_OUTCOMES_INVALID')
    return cid, mapping


def evaluate_metadata(slug, market, event, observed_ms):
    """Classify data availability at observation time, never signal profitability."""
    cid, mapping = identity(market, slug)
    start_ms = int(SLUG.fullmatch(slug)[1]) * 1000
    end_ms = start_ms + 300000
    phase = 'before_open' if observed_ms < start_ms else 'during_window' if observed_ms < end_ms else 'after_close'
    candidates, errors = [], []
    nodes = [(f'market.events[{i}]', e) for i, e in enumerate(market.get('events') or [])
             if isinstance(e, dict) and e.get('slug') == slug]
    if event is not None:
        if not isinstance(event, dict) or event.get('slug') != slug:
            errors.append('EVENT_SLUG_MISMATCH')
        else:
            matching = [m for m in event.get('markets', []) if isinstance(m, dict) and m.get('slug') == slug]
            if len(matching) != 1:
                errors.append('EVENT_MARKET_IDENTITY_MISSING')
            else:
                try:
                    if identity(matching[0], slug) != (cid, mapping):
                        errors.append('EVENT_MARKET_IDENTITY_CONFLICT')
                    else:
                        nodes.append(('event', event))
                except (ValueError, TypeError):
                    errors.append('EVENT_MARKET_IDENTITY_INVALID')
    has_final = False
    for path, node in nodes:
        meta = node.get('eventMetadata') or {}
        if not isinstance(meta, dict):
            errors.append('METADATA_INVALID'); continue
        if meta.get('finalPrice') not in (None, ''):
            has_final = True
        raw = meta.get('priceToBeat')
        if raw not in (None, ''):
            val = positive(raw)
            if val is None:
                errors.append('THRESHOLD_INVALID')
            else:
                candidates.append({'path': path + '.eventMetadata.priceToBeat', 'value': val})
    values = {Decimal(c['value']) for c in candidates}
    if len(values) > 1:
        errors.append('THRESHOLD_SOURCE_CONFLICT')
    value = str(next(iter(values))) if len(values) == 1 and not errors else None
    open_flags = market.get('closed') is False and market.get('acceptingOrders') is True and market.get('active') is True
    preclose = bool(value and phase == 'during_window' and open_flags and not has_final and not errors)
    reason = 'OBSERVED_IN_OPEN_WINDOW' if preclose else (
        'IDENTITY_OR_VALUE_CONFLICT' if errors else
        'PRICE_TO_BEAT_ABSENT' if not value else
        'POST_CLOSE_REFERENCE_ONLY' if phase == 'after_close' else
        'BEFORE_WINDOW' if phase == 'before_open' else
        'FINAL_PRICE_PRESENT_DURING_WINDOW' if has_final else 'NOT_ACCEPTING_ORDERS')
    return dict(slug=slug, condition_id=cid, tokens=mapping, observed_ms=observed_ms,
                market_start_ms=start_ms, market_end_ms=end_ms, phase=phase,
                published_price_to_beat=value, threshold_candidates=candidates,
                threshold_observed_before_close=preclose, active=market.get('active'),
                closed=market.get('closed'), accepting_orders=market.get('acceptingOrders'),
                final_price_field_present=has_final, reason=reason, errors=errors,
                historical_first_availability_ms=None, outcome_or_cash_receipt=False,
                strategy_or_order_enabled=False)


def context_observation(payload, condition_id):
    if not isinstance(payload, dict):
        return dict(itode=None, identity='invalid')
    response_id = payload.get('condition_id', payload.get('conditionId'))
    if response_id is not None and response_id != condition_id:
        return dict(itode=None, identity='conflict')
    val = payload.get('itode')
    return dict(itode=val if type(val) is bool else None,
                identity='response_and_requested_condition' if response_id else 'requested_condition_only',
                minimum_order_size=payload.get('minimum_order_size'),
                minimum_tick_size=payload.get('minimum_tick_size'))


def book_observation(payload, token, condition_id):
    if not isinstance(payload, dict) or str(payload.get('asset_id')) != token:
        return dict(identity='conflict', min_order_size=None, tick_size=None)
    if payload.get('market') not in (None, condition_id):
        return dict(identity='conflict', min_order_size=None, tick_size=None)
    return dict(identity='matched_token', min_order_size=positive(payload.get('min_order_size')),
                tick_size=positive(payload.get('tick_size')), timestamp=payload.get('timestamp'))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('REDIRECT_NOT_ALLOWED')


class PublicFetcher:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.bytes = 0
        self.rows = []

    def get(self, url):
        if not URL.fullmatch(url):
            raise ValueError('PUBLIC_ENDPOINT_NOT_ALLOWLISTED')
        row = dict(url=url, requested_ms=int(time.time()*1000), received_ms=None,
                   status=None, body_utf8=None, sha256=None, error=None)
        timer = time.monotonic()
        try:
            if self.stop.is_set():
                raise RuntimeError('PUBLIC_COLLECTION_HALTED')
            # No credentials and no environment proxy/redirect fallback.
            opener = build_opener(ProxyHandler({}), NoRedirect())
            for attempt in range(2):
                if self.stop.is_set():
                    raise RuntimeError('PUBLIC_COLLECTION_HALTED')
                try:
                    with opener.open(Request(url, headers={'User-Agent': 'reference-availability-audit/1.0'}), timeout=8) as r:
                        row['status'] = r.status
                        body = r.read(BODY_CAP+1)
                    if len(body) > BODY_CAP:
                        raise ValueError('RESPONSE_BYTE_CAP')
                    with self.lock:
                        self.bytes += len(body)
                        if self.bytes > TOTAL_CAP:
                            self.stop.set(); raise ValueError('TOTAL_BYTE_CAP')
                    row.update(body_utf8=body.decode('utf-8'), sha256=hashlib.sha256(body).hexdigest())
                    break
                except HTTPError as exc:
                    row['status'] = exc.code
                    if exc.code in (401, 403, 418, 429, 451):
                        self.stop.set()
                        raise RuntimeError('ACCESS_OR_RATE_RESTRICTION_NO_FALLBACK') from exc
                    if exc.code >= 500 and attempt == 0:
                        time.sleep(1)
                    else:
                        raise
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            row['error'] = str(exc)[:240]
        row.update(received_ms=int(time.time()*1000), roundtrip_ms=round((time.monotonic()-timer)*1000, 3))
        with self.lock:
            self.rows.append(row)
        return row


def payload(row):
    if row.get('error') or row.get('body_utf8') is None:
        return None
    try:
        return json.loads(row['body_utf8'])
    except (ValueError, TypeError):
        return None


def summarize(observations, responses):
    markets = {}
    for o in observations:
        if not o.get('condition_id'):
            continue
        m = markets.setdefault(o['slug'], dict(slug=o['slug'], open_observations=0, open_threshold_observations=0,
             first_threshold_observed_ms=None, first_open_threshold_observed_ms=None, observed_values=[], itode_values=[]))
        if o['phase'] == 'during_window':
            m['open_observations'] += 1
        if o['published_price_to_beat'] is not None:
            m['first_threshold_observed_ms'] = m['first_threshold_observed_ms'] or o['observed_ms']
            if o['published_price_to_beat'] not in m['observed_values']:
                m['observed_values'].append(o['published_price_to_beat'])
        if o['threshold_observed_before_close']:
            m['open_threshold_observations'] += 1
            m['first_open_threshold_observed_ms'] = m['first_open_threshold_observed_ms'] or o['observed_ms']
        itode = o.get('clob_context', {}).get('itode')
        if type(itode) is bool and itode not in m['itode_values']:
            m['itode_values'].append(itode)
    return dict(scope='public_reference_and_constraint_observations_only',
                observations=len(observations), http_responses=len(responses),
                http_errors=sum(bool(r.get('error')) for r in responses),
                data_error_observations=sum(bool(o.get('errors')) for o in observations),
                market_phases=list(markets.values()),
                open_threshold_observations=sum(m['open_threshold_observations'] for m in markets.values()),
                first_availability_is_sampled_not_exact=True,
                no_observation_is_not_proof_of_unavailability=True,
                latency_scope='public_HTTP_roundtrip_not_order_submission_latency',
                retrospective_training_allowed=False, account_cash_verified=False,
                strategy_evaluated=False, trading_enabled=False)


def verify_directory(root):
    manifest = json.loads((root/'manifest.json').read_text())
    checked = 0
    for name, digest in manifest['files'].items():
        if Path(name).name != name or hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
            raise ValueError('OUTPUT_SHA256_MISMATCH')
    for line in (root/'http_responses.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row.get('body_utf8') is not None:
            if hashlib.sha256(row['body_utf8'].encode('utf-8')).hexdigest() != row['sha256']:
                raise ValueError('RESPONSE_SHA256_MISMATCH')
            json.loads(row['body_utf8']); checked += 1
    return dict(verified_response_bodies=checked, verified_files=len(manifest['files']), external_truth_verified=False)


def run(output, seconds=330, interval=10):
    if not 60 <= seconds <= 600 or not 10 <= interval <= 30:
        raise ValueError('BOUNDED_DURATION_AND_INTERVAL_REQUIRED')
    if output.exists() and any(output.iterdir()):
        raise ValueError('NEW_OUTPUT_DIRECTORY_REQUIRED')
    output.mkdir(parents=True, exist_ok=True)
    fetcher, observations = PublicFetcher(), []
    begin = time.monotonic()
    deadline = begin
    with ThreadPoolExecutor(max_workers=3) as pool:
        while time.monotonic()-begin < seconds and not fetcher.stop.is_set():
            start = int(time.time())//300*300
            slugs = [f'btc-updown-5m-{start-300}', f'btc-updown-5m-{start}']
            jobs = [(slug, kind, pool.submit(fetcher.get, f'https://gamma-api.polymarket.com/{kind}/slug/{slug}'))
                    for slug in slugs for kind in ('markets', 'events')]
            grouped = {slug: {} for slug in slugs}
            for slug, kind, future in jobs:
                grouped[slug][kind] = future.result()
            for slug in slugs:
                rs = grouped[slug]
                try:
                    obs = evaluate_metadata(slug, payload(rs['markets']), payload(rs['events']),
                                            max(r['received_ms'] for r in rs.values()))
                    obs['metadata_urls'] = {k:r['url'] for k,r in rs.items()}
                    obs['metadata_http_errors'] = {k:r['error'] for k,r in rs.items() if r['error']}
                    if obs['phase'] == 'during_window' and not fetcher.stop.is_set():
                        cid, tokens = obs['condition_id'], obs['tokens']
                        ctx = pool.submit(fetcher.get, 'https://clob.polymarket.com/clob-markets/'+cid)
                        books = {side:pool.submit(fetcher.get, 'https://clob.polymarket.com/book?token_id='+token)
                                 for side,token in tokens.items()}
                        cr = ctx.result()
                        obs['clob_context'] = context_observation(payload(cr), cid)
                        obs['clob_context']['observed_ms'] = cr['received_ms']
                        obs['books'] = {}
                        for side,future in books.items():
                            br = future.result()
                            obs['books'][side] = book_observation(payload(br), tokens[side], cid)
                            obs['books'][side]['observed_ms'] = br['received_ms']
                except (ValueError, TypeError, KeyError) as exc:
                    obs = dict(slug=slug, errors=[str(exc)], observed_ms=int(time.time()*1000))
                observations.append(obs)
            # Checkpoints retain partial evidence if a later network call fails.
            for name, rows in [('http_responses.jsonl',fetcher.rows), ('observations.jsonl',observations)]:
                (output/name).write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in rows))
            deadline += interval
            if deadline < time.monotonic():
                deadline = time.monotonic()
            time.sleep(max(0, min(deadline, begin+seconds)-time.monotonic()))
    summary = summarize(observations, fetcher.rows)
    summary['elapsed_seconds'] = round(time.monotonic()-begin, 3)
    summary['collection_halted'] = fetcher.stop.is_set()
    (output/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True))
    manifest = dict(files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()},
                    created_ms=int(time.time()*1000), scope='read_only_public_evidence', orders_enabled=False)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True))
    print(json.dumps(summary,indent=2,sort_keys=True),flush=True)
    print(json.dumps(verify_directory(output),sort_keys=True),flush=True)
    if fetcher.stop.is_set() or not summary['market_phases'] or summary['http_errors']:
        raise RuntimeError('Incomplete public probe; retained diagnostic output, no fallback')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('reference_availability'))
    parser.add_argument('--seconds',type=int,default=330)
    parser.add_argument('--interval',type=int,default=10)
    parser.add_argument('--verify',action='store_true')
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_directory(args.output),indent=2))
    else:
        run(args.output,args.seconds,args.interval)
