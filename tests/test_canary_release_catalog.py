"""Synthetic catalog pagination with the real request loop; no external API.

Compare selection against the previous 100-record algorithm on a stable fixture.
Neither malformed pages nor transport failures may finalize a partial plan.
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
import forward_acquire as f


def item(n, capture=True):
    return {'id':n,'tag_name':f'capture-v2-{n}-1' if capture else f'other-{n}',
            'published_at':f'2026-01-01T00:{n:05d}', 'assets':[]}


def old_selection(rows, count):
    found=[]
    for offset in range(0,500,100):
        batch=rows[offset:offset+100]
        found.extend(r for r in batch if f.RELEASE_RE.fullmatch(str(r.get('tag_name',''))))
        if len(batch)<100 or len(found)>=count:break
    return sorted(found,key=lambda r:str(r.get('published_at','')),reverse=True)[:count]


class ReleaseCatalogTests(unittest.TestCase):
    def make_opener(self,rows, fail_first=False):
        self.urls=[]
        def opened(req,timeout):
            self.urls.append(req.full_url)
            q=parse_qs(urlparse(req.full_url).query)
            self.assertEqual(q.get('per_page'),['25'])
            self.assertEqual(timeout,30)
            if fail_first and len(self.urls)==1:
                raise urllib.error.HTTPError(req.full_url,504,'synthetic timeout',{},None)
            page=int(q['page'][0]);body=json.dumps(rows[(page-1)*25:page*25]).encode()
            response=io.BytesIO(body);response.headers={'Content-Length':str(len(body))}
            return response
        op=Mock();op.open.side_effect=opened;return op

    def run_catalog(self,rows,count=16,fail_first=False):
        op=self.make_opener(rows,fail_first)
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep'):
            return f.list_capture_releases(count)

    def test_full_logical_page_before_selection_not_first_small_page(self):
        rows=[item(i) for i in range(1,101)]
        result=self.run_catalog(rows)
        self.assertEqual(result,old_selection(rows,16))
        self.assertEqual(len(self.urls),4)
        self.assertEqual(result[0]['id'],100)

    def test_mixed_catalog_preserves_previous_logical_stop(self):
        rows=[item(i,capture=(i%10==0)) for i in range(1,301)]
        self.assertEqual(self.run_catalog(rows),old_selection(rows,16))
        self.assertEqual(len(self.urls),8)

    def test_short_last_page_preserves_candidate_set(self):
        rows=[item(i) for i in range(1,37)]
        self.assertEqual(self.run_catalog(rows),old_selection(rows,16))
        self.assertEqual(len(self.urls),2)

    def test_exact_small_page_boundary_checks_exhaustion(self):
        rows=[item(i) for i in range(1,26)]
        self.assertEqual(self.run_catalog(rows),old_selection(rows,16))
        self.assertEqual(len(self.urls),2)

    def test_empty_catalog_stays_empty(self):
        self.assertEqual(self.run_catalog([]),[])
        self.assertEqual(len(self.urls),1)

    def test_scan_bound_remains_five_hundred_records(self):
        rows=[item(i,capture=False) for i in range(1,601)]
        rows[550]=item(551)
        self.assertEqual(self.run_catalog(rows),[])
        self.assertEqual(len(self.urls),20)

    def test_retry_and_pages_share_twenty_request_attempts(self):
        rows=[item(i,capture=False) for i in range(1,601)]
        with self.assertRaisesRegex(f.TransportError,'RELEASE_DISCOVERY_ATTEMPT_BUDGET'):
            self.run_catalog(rows,fail_first=True)
        self.assertEqual(len(self.urls),20)

    def test_transient_first_page_error_can_recover_with_same_page(self):
        rows=[item(i) for i in range(1,101)]
        self.assertEqual(self.run_catalog(rows,fail_first=True),old_selection(rows,16))
        self.assertEqual(len(self.urls),5)
        self.assertEqual(self.urls[0],self.urls[1])

    def test_persistent_timeout_is_four_attempts_not_infinite_page_fallback(self):
        op=Mock();op.open.side_effect=urllib.error.HTTPError('https://example.invalid',504,'synthetic',{},None)
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep'):
            with self.assertRaises(f.TransportError) as caught:f.list_capture_releases(16)
        self.assertEqual(caught.exception.code,504);self.assertEqual(caught.exception.attempts,4)
        self.assertEqual(op.open.call_count,4)

    def test_access_refusal_stops_without_another_page(self):
        op=Mock();op.open.side_effect=urllib.error.HTTPError('https://example.invalid',403,'synthetic',{},None)
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaises(f.AccessRefused):f.list_capture_releases(16)
        self.assertEqual(op.open.call_count,1);sleep.assert_not_called()

    def test_repeated_identity_rejects_shifted_catalog(self):
        rows=[item(i) for i in range(1,51)];rows[25]=dict(rows[24])
        with self.assertRaisesRegex(ValueError,'RELEASE_PAGINATION_IDENTITY_REPEATED'):
            self.run_catalog(rows)
        self.assertEqual(len(self.urls),2)

    def test_malformed_identity_cannot_create_a_selection(self):
        for record in ({'id':True,'tag_name':'capture-v2-1-1'}, {'tag_name':'capture-v2-1-1'},'wrong'):
            with self.subTest(record=record):
                with self.assertRaisesRegex(ValueError,'RELEASE_IDENTITY_SCHEMA'):
                    self.run_catalog([record])

    def test_failed_partial_discovery_leaves_no_frozen_plan(self):
        rows=[item(i) for i in range(1,101)]
        op=self.make_opener(rows)
        normal=op.open.side_effect
        def opened(req,timeout):
            if 'page=2' in req.full_url:
                raise urllib.error.HTTPError(req.full_url,504,'synthetic',{},None)
            return normal(req,timeout)
        op.open.side_effect=opened
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep'):
                with self.assertRaises(f.TransportError):f.acquire(Path(directory),16,100)
            manifest=json.loads((Path(directory)/'acquisition_manifest.json').read_text())
            self.assertFalse(manifest['selection_complete']);self.assertEqual(manifest['files'],[])
            self.assertFalse((Path(directory)/'acquisition_plan.json').exists())

    def test_invalid_limit_rejected_before_network(self):
        with patch.object(f.urllib.request,'build_opener') as opened:
            for count in (0,-1,True,'16'):
                with self.subTest(count=count):
                    with self.assertRaises(ValueError):f.list_capture_releases(count)
            opened.assert_not_called()

    def test_budget_cannot_issue_twenty_first_attempt(self):
        budget=f.DiscoveryBudget()
        for _ in range(20):budget.consume()
        with self.assertRaises(f.TransportError):budget.consume()
        self.assertEqual((budget.used,budget.remaining),(20,0))

if __name__=='__main__':unittest.main()
