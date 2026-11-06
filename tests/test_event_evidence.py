import contextlib,gzip,hashlib,http.client,io,json,sys,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
import acquire_event_evidence as a
class EventEvidenceTests(unittest.TestCase):
 def test_name(self):
  self.assertTrue(a.safe_name('raw-2026-09-23-000002.jsonl.gz'));self.assertFalse(a.safe_name('../raw-2026-09-23-000002.jsonl.gz'))
 def test_only_raw(self):self.assertEqual(a.select_assets([{'name':'snapshots-2026-09-23-000002.jsonl.gz','size':1}])[0],[])
 def test_budget(self):
  chosen,skip=a.select_assets([{'name':'raw-2026-09-23-000002.jsonl.gz','size':20},{'name':'raw-2026-09-23-000001.jsonl.gz','size':2}],budget=10)
  self.assertEqual(chosen[0]['size'],2);self.assertEqual(len(skip),1)
 def test_latest_two(self):
  g=[dict(name=f'raw-2026-09-23-{i:06d}.jsonl.gz',size=1)for i in range(5)]
  chosen,_=a.select_assets(g);self.assertEqual([x['name']for x in chosen],[g[4]['name'],g[3]['name']])
 def test_checksum_and_counts(self):
  body=gzip.compress((json.dumps({'source':'public','received_at_ns':9})+'\n').encode())
  r=a.verify_and_describe(body,hashlib.sha256(body).hexdigest());self.assertEqual(r['rows'],1);self.assertEqual(r['first_received_ns'],9)
 def test_bad_checksum(self):
  with self.assertRaises(ValueError):a.verify_and_describe(gzip.compress(b'{}\n'),'bad')
 def test_unexpected_source(self):
  with self.assertRaises(ValueError):a.read_public('https://example.com/secrets',100)

class TransportTests(unittest.TestCase):
 URL=a.API+'releases/1/assets?per_page=100&page=1'
 def response(self,body=b'[]',headers=None):
  r=io.BytesIO(body);r.headers=headers or {};return r
 def test_transient_http_retry_keeps_exact_request(self):
  failure=urllib.error.HTTPError(self.URL,503,'private error',{},None)
  with patch.object(a.urllib.request,'urlopen',side_effect=[failure,self.response()]) as request,patch.object(a.time,'sleep') as sleep:
   self.assertEqual(a.read_public(self.URL,20),b'[]')
  self.assertEqual([call.args[0].full_url for call in request.call_args_list],[self.URL]*2)
  self.assertEqual(sleep.call_count,1)
 def test_transport_exhaustion_is_bounded_and_sanitized(self):
  with patch.object(a.urllib.request,'urlopen',side_effect=TimeoutError('DO_NOT_DISCLOSE')) as request,patch.object(a.time,'sleep') as sleep:
   with self.assertRaises(a.TransportError) as caught:a.read_public(self.URL,20)
  self.assertEqual(caught.exception.attempts,4)
  self.assertNotIn('DO_NOT_DISCLOSE',str(caught.exception))
  self.assertEqual(request.call_count,4);self.assertEqual(sleep.call_count,3)
 def test_refusals_and_not_found_are_not_retried(self):
  for code in (401,403,404,418,429,451):
   with self.subTest(code=code),patch.object(a.urllib.request,'urlopen',side_effect=urllib.error.HTTPError(self.URL,code,'denied',{},None)) as request,patch.object(a.time,'sleep') as sleep:
    with self.assertRaises(a.TransportError) as caught:a.read_public(self.URL,20)
    self.assertEqual(caught.exception.code,code)
    self.assertEqual(request.call_count,1);sleep.assert_not_called()
 def test_short_body_and_incomplete_read_share_retry_budget(self):
  failures=[self.response(b'[]',{'Content-Length':'8'}),http.client.IncompleteRead(b'private',4),self.response()]
  with patch.object(a.urllib.request,'urlopen',side_effect=failures) as request,patch.object(a.time,'sleep'):
   self.assertEqual(a.read_public(self.URL,20),b'[]')
  self.assertEqual(request.call_count,3)
 def test_invalid_json_and_http_errors_share_four_attempts(self):
  failures=[self.response(b'{'),urllib.error.HTTPError(self.URL,502,'server',{},None),self.response(b'\xff'),self.response(b'{')]
  with patch.object(a.urllib.request,'urlopen',side_effect=failures) as request,patch.object(a.time,'sleep') as sleep:
   with self.assertRaises(a.ResponseError) as caught:a.read_public(self.URL,20,json_response=True)
  self.assertEqual(caught.exception.attempts,4)
  self.assertEqual(request.call_count,4);self.assertEqual(sleep.call_count,3)
 def test_size_limits_are_not_retried(self):
  for response in (self.response(b'large'),self.response(b'',{'Content-Length':'99'})):
   with patch.object(a.urllib.request,'urlopen',return_value=response) as request,patch.object(a.time,'sleep') as sleep:
    with self.assertRaises(ValueError):a.read_public(self.URL,2)
    self.assertEqual(request.call_count,1);sleep.assert_not_called()

class PublicStatusTests(unittest.TestCase):
 def test_status_has_no_exception_message_or_unknown_phase(self):
  marker='SYNTHETIC_DO_NOT_DISCLOSE'
  status=a.public_status({'phase':marker},ValueError(marker))
  self.assertNotIn(marker,json.dumps(status))
  self.assertFalse(status['completed']);self.assertEqual(status['phase'],'unknown')
  self.assertFalse(status['strategy_evaluated']);self.assertFalse(status['live_trading_enabled'])
 def test_http_failure_remains_failed_with_typed_details(self):
  status=a.public_status({'phase':'segment_download'},a.TransportError(503,True,4))
  self.assertFalse(status['completed']);self.assertEqual(status['http_status'],503)
  self.assertEqual(status['attempts'],4);self.assertTrue(status['retryable'])
 def test_refusal_diagnostic_never_becomes_retryable(self):
  status=a.public_status({'phase':'asset_listing'},a.AccessRefused(403,False,1))
  self.assertTrue(status['access_refused']);self.assertFalse(status['retryable'])
 def test_success_is_only_sample_integrity(self):
  status=a.public_status({'phase':'evidence_save'})
  self.assertTrue(status['completed']);self.assertFalse(status['full_tape_complete'])
  self.assertFalse(status['live_trading_enabled']);self.assertIsNone(status['failure_category'])
 def test_cli_writes_status_and_preserves_failure_exit(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'status.json'
   with patch.object(a,'acquire',side_effect=a.TransportError(502,True,4)):
    with self.assertRaises(a.TransportError):
     a.main(['--manifest','unused','--output',directory,'--status-file',str(path)])
   status=json.loads(path.read_text())
  self.assertFalse(status['completed']);self.assertEqual(status['http_status'],502)
 def test_checksum_failure_is_not_retried_or_reselected(self):
  with tempfile.TemporaryDirectory() as directory:
   manifest=Path(directory)/'manifest.json'
   manifest.write_text(json.dumps({'source_repository':'yt-feng/poly','releases':[{'id':1,'tag':'capture-v2-1-1','published_at':'2026-01-01'}]}))
   name='raw-2026-01-01-000001.jsonl.gz';url=a.DOWNLOAD+'capture-v2-1-1/'+name
   assets=[{'name':name,'size':5,'id':1,'browser_download_url':url},{'name':name+'.sha256','browser_download_url':url+'.sha256'}]
   with patch.object(a,'read_public',side_effect=[assets,b'0'*64,b'bad']) as request,contextlib.redirect_stdout(io.StringIO()):
    with self.assertRaisesRegex(ValueError,'Source SHA256 mismatch'):a.acquire(manifest,Path(directory)/'out')
   self.assertEqual(request.call_count,3)
if __name__=='__main__':unittest.main()
