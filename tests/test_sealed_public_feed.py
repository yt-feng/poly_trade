"""Synthetic fixtures only. No live endpoints, actual addresses, or private logs."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from research_vault import recipient_for, unseal, load_json_bytes
from sealed_public_feed import collect, validate_request, parse_page, next_query, market_units, profile_counts, MAX_BODY


def page(rows=None, more=False, cursor=None):
    return json.dumps({'data': [{'fixture': True}] if rows is None else rows,
                       'pagination': {'has_more': more, 'next_cursor': cursor}}).encode()


def request(**changes):
    value = {'endpoint': '/v2/trades', 'filters': {'user': 'synthetic-wallet', 'start': 1, 'end': 9, 'limit': 2}, 'max_pages': 2}
    value.update(changes)
    return value


class FeedContract(unittest.TestCase):
    def setUp(self):
        self.key = X25519PrivateKey.generate()
        self.recipient = recipient_for(self.key)
        self.rid = self.recipient['recipient_id']
        self.t = 100
    def clock(self):
        self.t += 1
        return self.t
    def run_collect(self, req=None, fetcher=None):
        encrypted = collect(req or request(), self.recipient, self.rid,
                            fetcher or (lambda ep, q: (200, page())), self.clock)
        self.assertNotIn(b'synthetic-wallet', encrypted)
        return load_json_bytes(unseal(encrypted, self.key))
    def test_missing_recipient_prevents_network(self):
        calls = []
        with self.assertRaises(ValueError):
            collect(request(), {}, self.rid, lambda *x: calls.append(x))
        self.assertEqual(calls, [])
    def test_wrong_fingerprint_prevents_network(self):
        calls = []
        with self.assertRaises(ValueError):
            collect(request(), self.recipient, '0'*64, lambda *x: calls.append(x))
        self.assertEqual(calls, [])
    def test_empty_page_is_not_unknown(self):
        self.assertEqual(parse_page(page([], False, None)), ([], None, False))
    def test_null_data_not_empty_list(self):
        with self.assertRaises(ValueError):
            parse_page(b'{"data":null,"pagination":{"has_more":false,"next_cursor":null}}')
    def test_legacy_array_rejected(self):
        with self.assertRaises(ValueError): parse_page(b'[]')
    def test_missing_pagination_rejected(self):
        with self.assertRaises(ValueError): parse_page(b'{"data":[]}')
    def test_nonboolean_has_more_rejected(self):
        with self.assertRaises(ValueError): parse_page(page([], 0, None))
    def test_more_without_cursor_rejected(self):
        with self.assertRaises(ValueError): parse_page(page(more=True))
    def test_false_with_cursor_rejected(self):
        with self.assertRaises(ValueError): parse_page(page(cursor='next'))
    def test_empty_with_more_continues(self):
        self.assertEqual(parse_page(page([], True, 'next')), ([], 'next', True))
    def test_oversized_body_rejected(self):
        with self.assertRaises(ValueError): parse_page(b' '* (MAX_BODY + 1))
    def test_offset_forbidden(self):
        with self.assertRaises(ValueError): validate_request(request(filters={'offset': 1}))
    def test_nonuser_time_filters_rejected(self):
        with self.assertRaises(ValueError): validate_request(request(filters={'condition': 'synthetic-condition', 'start': 1}))
    def test_global_feed_without_time_filter_allowed(self):
        self.assertEqual(validate_request(request(filters={'limit': 2}))[1], {'limit': 2})
    def test_activity_requires_user(self):
        with self.assertRaises(ValueError): validate_request(request(endpoint='/v2/activity', filters={}))
    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(ValueError): validate_request(request(endpoint='/order'))
    def test_page_budget_rejected(self):
        with self.assertRaises(ValueError): validate_request(request(max_pages=0))
    def test_bool_page_budget_rejected(self):
        with self.assertRaises(ValueError): validate_request(request(max_pages=True))
    def test_reversed_window_rejected(self):
        with self.assertRaises(ValueError): validate_request(request(filters={'user': 'synthetic', 'start': 9, 'end': 1}))
    def test_query_preserves_filters_and_input(self):
        f = request()['filters']
        self.assertEqual(next_query(f, 'x'), dict(f, cursor='x'))
        self.assertNotIn('cursor', f)
    def test_paginated_walk_preserves_full_filters(self):
        calls = []
        def fetch(ep, q):
            calls.append(q)
            return (200, page(more=True, cursor='token')) if len(calls) == 1 else (200, page())
        r = self.run_collect(fetcher=fetch)
        self.assertEqual(calls[1], dict(calls[0], cursor='token'))
        self.assertEqual(r['completion'], 'CURSOR_EXHAUSTED_FOR_REQUEST')
        self.assertFalse(r['all_history_complete'])
    def test_repeated_cursor_stops(self):
        r = self.run_collect(fetcher=lambda ep,q:(200,page(more=True,cursor='same')))
        self.assertEqual(r['completion'], 'VALIDATION_OR_TRANSPORT_STOP')
    def test_budget_stop_not_complete(self):
        r = self.run_collect(req=request(max_pages=1), fetcher=lambda ep,q:(200,page(more=True,cursor='next')))
        self.assertEqual(r['completion'], 'BUDGET_STOP')
    def test_rate_refusal_no_retry(self):
        calls=[]
        def fetch(ep,q): calls.append(q); return 429,b''
        self.assertEqual(self.run_collect(fetcher=fetch)['http_status'],429)
        self.assertEqual(len(calls),1)
    def test_access_refusal_no_fallback(self):
        calls=[]
        def fetch(ep,q): calls.append(q); return 403,b''
        r=self.run_collect(fetcher=fetch)
        self.assertEqual(r['completion'],'HTTP_STOP')
        self.assertEqual(len(calls),1)
    def test_error_message_not_retained(self):
        def fetch(ep,q): raise ValueError('SECRET_SYNTHETIC_ERROR')
        r=self.run_collect(fetcher=fetch)
        self.assertNotIn('SECRET_SYNTHETIC_ERROR',json.dumps(r))
    def test_response_received_time_recorded(self):
        p=self.run_collect()['observed_pages'][0]
        self.assertGreaterEqual(p['received_ms'],p['request_started_ms'])
        self.assertEqual(len(p['response_sha256']),64)
    def test_units_not_notional(self):
        r=market_units('10000','.05')
        self.assertEqual(r['gross_notional_usdc'],'500.00')
        self.assertFalse(r['pnl_known'])
    def test_missing_unit_is_unknown(self):
        self.assertIsNone(market_units(None,'.5')['gross_notional_usdc'])
    def test_nan_units_rejected(self):
        with self.assertRaises(ValueError): market_units('NaN','.5')
    def test_boolean_units_rejected(self):
        with self.assertRaises(ValueError): market_units(True,'.5')
    def test_profile_markets_not_fills_or_roi(self):
        r=profile_counts({'trades':12,'all_time_pnl':{'trade_count':34}})
        self.assertEqual(r['distinct_markets_traded'],12)
        self.assertEqual(r['fill_count'],34)
        self.assertIsNone(r['actual_roi'])
    def test_no_fill_count_inference(self):
        self.assertIsNone(profile_counts({'trades':12})['fill_count'])


class PublicationContracts(unittest.TestCase):
    def test_retired_publishers_have_no_execution_or_artifact_steps(self):
        root=Path(__file__).resolve().parents[1]/'.github/workflows'
        files=['canary-research.yml','canary-forward.yml','polymarket-eda.yml','import-run-24869603988-and-analyze.yml','run-24869603988-analysis.yml']
        for name in files:
            text=(root/name).read_text()
            self.assertIn('PLAINTEXT_PUBLICATION_RETIRED',text)
            for banned in ['contents: write','upload-artifact','GITHUB_STEP_SUMMARY','git push','python analysis/']:
                self.assertNotIn(banned,text)
    def test_collection_module_has_no_order_or_signing_dependency(self):
        text=(Path(__file__).resolve().parents[1]/'tools/sealed_public_feed.py').read_text()
        self.assertNotIn('import py_clob_client',text)
        self.assertNotIn('private_key=',text)
        self.assertIn("method='GET'",text)

if __name__=='__main__': unittest.main()
