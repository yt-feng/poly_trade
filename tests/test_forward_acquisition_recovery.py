"""Synthetic failure injection and real loopback HTTP; no market strategy."""
import contextlib
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'analysis'))
import forward_acquire as f


def fixture(name='snapshots-000001.jsonl.gz', body=b'abcd'):
    u='https://github.com/yt-feng/poly/releases/download/capture-v2-1-1/'+name
    check=hashlib.sha256(body).hexdigest().encode()+b'  '+name.encode()+b'\n'
    a={'name':name,'size':len(body),'browser_download_url':u,'created_at':'2026-01-01T00:00:00Z'}
    s={'name':name+'.sha256','size':len(check),'browser_download_url':u+'.sha256'}
    return [a,s], {u:body,u+'.sha256':check}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.out=Path(self.t.name)
        self.p=patch.object(f,'list_capture_releases',return_value=[{'id':1,'tag_name':'capture-v2-1-1','published_at':'2026-01-01T00:00:00Z'}]);self.p.start()
        self.assets,self.payload=fixture();a,b=fixture('labels-000002.jsonl.gz',b'efgh');self.assets+=a;self.payload.update(b)
        self.a=patch.object(f,'list_release_assets',return_value=self.assets);self.a.start()
        self.calls=[];self.failing=set()
        def fetch(url):
            self.calls.append(url)
            if url in self.failing:raise f.TransportError(500,True,4)
            return self.payload[url]
        self.r=patch.object(f,'request',side_effect=fetch);self.r.start()
    def tearDown(self):
        self.r.stop();self.a.stop();self.p.stop();self.t.cleanup()
    def run_a(self,**kw):return f.acquire(self.out,1,8,**kw)
    def test_success_plan_and_accounting(self):
        m=self.run_a();self.assertTrue(f.validate_manifest(m));self.assertEqual(m['downloaded_bytes'],8)
    def test_recovery_reuses_verified_files(self):
        bad=self.assets[2]['browser_download_url'];self.failing.add(bad)
        first=self.run_a();self.assertFalse(f.validate_manifest(first));self.assertEqual(len(first['files']),1)
        self.failing.clear();self.calls.clear();second=self.run_a(resume=True)
        self.assertTrue(f.validate_manifest(second));self.assertEqual(first['plan_sha256'],second['plan_sha256'])
        self.assertEqual(len(second['failure_history']),1);self.assertNotIn(self.assets[0]['browser_download_url'],self.calls)
    def test_recovery_does_not_reenumerate_new_market_sample(self):
        self.failing.add(self.assets[2]['browser_download_url']);self.run_a();self.failing.clear()
        with patch.object(f,'list_capture_releases',side_effect=AssertionError('new selection forbidden')),patch.object(f,'list_release_assets',side_effect=AssertionError('new selection forbidden')):
            self.assertTrue(f.validate_manifest(self.run_a(resume=True)))
    def test_failure_does_not_free_reserved_budget(self):
        self.failing.add(self.assets[2]['browser_download_url'])
        m=f.acquire(self.out,1,4);self.assertEqual(len(m['errors']),1);self.assertEqual(len(m['skipped']),1);self.assertEqual(m['files'],[])
    def test_unresolved_failure_stays_failed(self):
        self.failing.add(self.assets[2]['browser_download_url']);self.run_a()
        self.assertFalse(f.validate_manifest(self.run_a(resume=True)))
    def test_second_recovery_disallowed(self):
        self.run_a();self.run_a(resume=True)
        with self.assertRaises(ValueError):self.run_a(resume=True)
    def test_corrupt_local_data_never_silently_refetched(self):
        m=self.run_a();p=self.out/m['files'][0]['path'];p.write_bytes(b'evil');self.calls.clear()
        m=self.run_a(resume=True);self.assertFalse(f.validate_manifest(m));self.assertEqual(self.calls,[])
    def test_corrupt_sidecar_detected(self):
        m=self.run_a();p=self.out/m['files'][0]['path'];p.with_name(p.name+'.sha256').write_bytes(b'0'*64)
        self.assertFalse(f.validate_manifest(self.run_a(resume=True)))
    def test_plan_hash_mismatch(self):
        self.run_a();p=self.out/'acquisition_plan.json';v=json.loads(p.read_text());v['plan']['max_bytes']=9;p.write_text(json.dumps(v))
        with self.assertRaises(ValueError):self.run_a(resume=True)
    def test_changed_resume_budget(self):
        self.run_a()
        with self.assertRaises(ValueError):f.acquire(self.out,1,9,resume=True)
    def test_fresh_run_never_overwrites_plan(self):
        self.run_a()
        with self.assertRaises(ValueError):self.run_a()
    def test_size_mismatch_fails(self):
        self.payload[self.assets[0]['browser_download_url']]=b'abcdx'
        self.assertFalse(f.validate_manifest(self.run_a()))
    def test_checksum_mismatch_not_retryable(self):
        self.payload[self.assets[0]['browser_download_url']]=b'abce'
        m=self.run_a();self.calls.clear();m=self.run_a(resume=True)
        self.assertFalse(f.validate_manifest(m));self.assertEqual(self.calls,[])
    def test_sidecar_filename_mismatch(self):
        u=self.assets[1]['browser_download_url'];self.payload[u]=self.payload[u].replace(b'snapshots-000001',b'snapshots-000999')
        self.assertFalse(f.validate_manifest(self.run_a()))
    def test_api_digest_conflict(self):
        self.assets[0]['digest']='sha256:'+'0'*64
        self.assertFalse(f.validate_manifest(self.run_a()))
    def test_access_refusal_halts_all_subsequent_requests(self):
        with patch.object(f,'request',side_effect=f.AccessRefused(403,False)) as request:
            m=self.run_a();self.assertEqual(request.call_count,1)
            self.assertTrue(m['acquisition_halted']);self.assertFalse(f.validate_manifest(m))
    def test_duplicate_names_fail(self):
        self.assets.append(self.assets[0])
        with self.assertRaises(ValueError):self.run_a()
    def test_no_snapshot_not_success(self):
        self.assets[:]=self.assets[2:]
        self.assertFalse(f.validate_manifest(self.run_a()))
    def test_invalid_bool_limits(self):
        with self.assertRaises(ValueError):f.acquire(self.out,True,8)
    def test_empty_manifest_fails(self):self.assertFalse(f.validate_manifest({}))


class RequestTests(unittest.TestCase):
    URL='https://api.github.com/repos/yt-feng/poly/releases?per_page=100'
    def call(self,side_effect):
        obj=unittest.mock.Mock();obj.open.side_effect=side_effect
        return obj
    def response(self, body, headers=None):
        response=io.BytesIO(body);response.headers=headers or {}
        return response
    def test_truncated_json_then_success_uses_same_url(self):
        op=self.call([self.response(b'[{"tag_name":"capture-'), self.response(b'[]')])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            self.assertEqual(f.get_json(self.URL),[])
        self.assertEqual([c.args[0].full_url for c in op.open.call_args_list],[self.URL]*2)
        self.assertEqual(sleep.call_count,1)
    def test_parseable_short_body_is_not_accepted(self):
        op=self.call([self.response(b'[]',{'Content-Length':'20'}),self.response(b'[]',{'Content-Length':'2'})])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep'):
            self.assertEqual(f.get_json(self.URL),[])
        self.assertEqual(op.open.call_count,2)
    def test_invalid_utf8_then_success(self):
        op=self.call([self.response(b'["\xff"]'),self.response(b'[]')])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep'):
            self.assertEqual(f.get_json(self.URL),[])
        self.assertEqual(op.open.call_count,2)
    def test_json_and_http_failures_share_four_attempt_budget(self):
        op=self.call([urllib.error.HTTPError(self.URL,500,'server',{},None),
            self.response(b'{'),urllib.error.HTTPError(self.URL,502,'server',{},None),self.response(b'{')])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaises(f.ResponseError) as caught:f.get_json(self.URL)
        self.assertEqual(caught.exception.attempts,4)
        self.assertEqual(str(caught.exception),'INVALID_JSON_RESPONSE')
        self.assertEqual(op.open.call_count,4);self.assertEqual(sleep.call_count,3)
    def test_response_cap_is_not_retried(self):
        op=self.call([self.response(b'[]',{'Content-Length':str(f.RESPONSE_CAP+1)})])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'RESPONSE_BYTE_CAP'):f.get_json(self.URL)
        self.assertEqual(op.open.call_count,1);sleep.assert_not_called()
    def test_valid_json_with_wrong_schema_is_not_retried(self):
        op=self.call([self.response(b'{}')])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'RELEASE_SCHEMA'):f.list_capture_releases(1)
        self.assertEqual(op.open.call_count,1);sleep.assert_not_called()
    def test_json_refusal_stops_after_prior_malformed_response(self):
        op=self.call([self.response(b'{'),urllib.error.HTTPError(self.URL,429,'rate',{},None)])
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaises(f.AccessRefused):f.get_json(self.URL)
        self.assertEqual(op.open.call_count,2);self.assertEqual(sleep.call_count,1)
    def test_incomplete_read_exhaustion_is_sanitized(self):
        op=self.call(http.client.IncompleteRead(b'private-response-content',100))
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaises(f.ResponseError) as caught:f.get_json(self.URL)
        self.assertEqual(str(caught.exception),'INCOMPLETE_HTTP_RESPONSE')
        self.assertEqual(caught.exception.attempts,4);self.assertEqual(sleep.call_count,3)
    def test_rate_refusal_not_retried(self):
        op=self.call(urllib.error.HTTPError(self.URL,429,'rate',{},None))
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaises(f.AccessRefused):f.request(self.URL)
            self.assertEqual(op.open.call_count,1);sleep.assert_not_called()
    def test_500_retry_limit_and_no_final_sleep(self):
        op=self.call(urllib.error.HTTPError(self.URL,500,'server',{},None))
        with patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
            with self.assertRaises(f.TransportError) as c:f.request(self.URL)
            self.assertTrue(c.exception.retryable);self.assertEqual(op.open.call_count,4);self.assertEqual(sleep.call_count,3)
    def test_404_not_retried(self):
        op=self.call(urllib.error.HTTPError(self.URL,404,'absent',{},None))
        with patch.object(f.urllib.request,'build_opener',return_value=op):
            with self.assertRaises(f.TransportError) as c:f.request(self.URL)
            self.assertFalse(c.exception.retryable);self.assertEqual(op.open.call_count,1)
    def test_retry_after_respected(self):self.assertEqual(f._retry_wait({'Retry-After':'12'},0),12)
    def test_long_retry_after_stops(self):
        with self.assertRaises(f.TransportError):f._retry_wait({'Retry-After':'3600'},0)
    def test_unexpected_source(self):
        with self.assertRaises(ValueError):f.request('https://example.test/anything')
    def test_redirect_strips_credentials(self):
        r=urllib.request.Request(self.URL,headers={'Authorization':'Bearer synthetic-test'})
        n=f.SafeRedirect().redirect_request(r,None,302,'',{},'https://release-assets.githubusercontent.com/a')
        self.assertIsNone(n.get_header('Authorization'))
    def test_unexpected_redirect_blocked(self):
        with self.assertRaises(ValueError):f.SafeRedirect().redirect_request(urllib.request.Request(self.URL),None,302,'',{},'https://example.test/a')
    def test_real_http_500_then_success(self):
        counts={'n':0}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                counts['n']+=1
                if counts['n']<=2:self.send_response(500);self.end_headers();return
                self.send_response(200);self.send_header('Content-Length','4');self.end_headers();self.wfile.write(b'data')
            def log_message(self,*a):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        real=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        class LocalGateway:
            def open(self,req,timeout):return real.open('http://127.0.0.1:'+str(server.server_port)+'/',timeout=timeout)
        try:
            with patch.object(f.urllib.request,'build_opener',return_value=LocalGateway()),patch.object(f.time,'sleep'):
                self.assertEqual(f.request(self.URL),b'data');self.assertEqual(counts['n'],3)
        finally:server.shutdown();server.server_close();thread.join()
    def test_real_truncated_content_length_and_chunked_json_then_success(self):
        counts={'n':0}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                counts['n']+=1
                self.send_response(200)
                if counts['n']==1:
                    self.send_header('Content-Length','100');self.end_headers();self.wfile.write(b'[]')
                elif counts['n']==2:
                    self.send_header('Transfer-Encoding','chunked');self.end_headers();self.wfile.write(b'4\r\nab')
                else:
                    self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'[]')
            def log_message(self,*a):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        real=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        class LocalGateway:
            def open(self,req,timeout):return real.open('http://127.0.0.1:'+str(server.server_port)+'/',timeout=timeout)
        try:
            with patch.object(f.urllib.request,'build_opener',return_value=LocalGateway()),patch.object(f.time,'sleep') as sleep:
                self.assertEqual(f.get_json(self.URL),[])
                self.assertEqual(counts['n'],3);self.assertEqual(sleep.call_count,2)
        finally:server.shutdown();server.server_close();thread.join()


class PlanningFailureTests(unittest.TestCase):
    def test_discovery_failure_keeps_evidence_without_an_extra_recovery_pass(self):
        op=unittest.mock.Mock()
        def malformed(*args,**kwargs):
            response=io.BytesIO(b'[{"body":"private-response-content')
            response.headers={}
            return response
        op.open.side_effect=malformed
        with tempfile.TemporaryDirectory() as td:
            args=['forward_acquire','--output',td,'--max-releases','1','--max-bytes','8','--recover-once']
            with patch.object(sys,'argv',args),patch.object(f.urllib.request,'build_opener',return_value=op),patch.object(f.time,'sleep') as sleep:
                with self.assertRaises(f.ResponseError):f.main()
            manifest_path=Path(td)/'acquisition_manifest.json'
            manifest=json.loads(manifest_path.read_text())
            self.assertFalse(f.validate_manifest(manifest));self.assertTrue(manifest['acquisition_halted'])
            self.assertFalse(manifest['selection_complete']);self.assertEqual(manifest['files'],[])
            self.assertEqual(len(manifest['errors']),1)
            self.assertEqual(manifest['errors'][0]['error'],'INVALID_JSON_RESPONSE')
            self.assertEqual(manifest['errors'][0]['attempts'],4)
            self.assertEqual(manifest['errors'][0]['stage'],'selection_plan')
            self.assertNotIn('private-response-content',manifest_path.read_text())
            self.assertFalse((Path(td)/'acquisition_plan.json').exists())
            self.assertFalse((Path(td)/'acquisition_state.json').exists())
            self.assertEqual(op.open.call_count,4);self.assertEqual(sleep.call_count,3)
    def test_partial_asset_discovery_does_not_publish_an_incomplete_plan(self):
        releases=[{'id':n,'tag_name':f'capture-v2-{n}-1'} for n in (1,2)]
        with tempfile.TemporaryDirectory() as td,patch.object(f,'list_capture_releases',return_value=releases),patch.object(f,'list_release_assets',side_effect=[fixture()[0],f.ResponseError('INVALID_JSON_RESPONSE',4)]),patch.object(f,'request') as request:
            with self.assertRaises(f.ResponseError):f.acquire(Path(td),2,8)
            manifest=json.loads((Path(td)/'acquisition_manifest.json').read_text())
            self.assertFalse(f.validate_manifest(manifest));self.assertEqual(manifest['releases'],[])
            self.assertEqual(manifest['errors'][0]['attempts'],4)
            self.assertFalse((Path(td)/'acquisition_plan.json').exists());request.assert_not_called()
    def test_access_refusal_is_recorded_and_never_downloads(self):
        with tempfile.TemporaryDirectory() as td,patch.object(f,'list_capture_releases',side_effect=f.AccessRefused(403,False)) as discovery,patch.object(f,'request') as request:
            with self.assertRaises(f.AccessRefused):f.acquire(Path(td),1,8)
            manifest=json.loads((Path(td)/'acquisition_manifest.json').read_text())
            self.assertTrue(manifest['errors'][0]['access_refused'])
            self.assertEqual(manifest['errors'][0]['http_status'],403)
            self.assertFalse(f.validate_manifest(manifest));self.assertEqual(discovery.call_count,1)
            request.assert_not_called()


class WorkflowContracts(unittest.TestCase):
    def test_no_greenwashing_and_independent_steps(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/canary-forward-audit.yml').read_text()
        self.assertNotIn('continue-on-error',text);self.assertIn('--recover-once',text)
        self.assertGreaterEqual(text.count("!cancelled() && steps.regression.outcome == 'success'"),4)
        self.assertIn("if not ok:raise SystemExit",text)

if __name__=='__main__':unittest.main()
