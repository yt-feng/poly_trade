import gzip,hashlib,json,sys,unittest
from pathlib import Path
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
if __name__=='__main__':unittest.main()
