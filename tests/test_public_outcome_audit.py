import sys,unittest,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from public_outcome_audit import parse_evidence,fetch_public
S='btc-updown-5m-1790301900';C='0x'+'a'*64
class OutcomeTests(unittest.TestCase):
 def fixture(self):
  g=dict(slug=S,conditionId=C,clobTokenIds='["1","2"]',outcomes='["Up","Down"]',closed=True,umaResolutionStatus='resolved',outcomePrices='["1","0"]')
  c=dict(condition_id=C,closed=True,tokens=[dict(token_id='1',outcome='Up',winner=True),dict(token_id='2',outcome='Down',winner=False)])
  return g,c
 def parse(self,g,c):return parse_evidence(S,g,c,1790303000000)
 def test_clob(self):g,c=self.fixture();self.assertEqual(self.parse(g,c)['winner'],'up')
 def test_gamma(self):g,c=self.fixture();self.assertEqual(self.parse(g,{})['source'],'gamma_resolved_exact_payout')
 def test_prices_not_resolution(self):
  g,c=self.fixture();g['umaResolutionStatus']='proposed';self.assertRaises(ValueError,self.parse,g,{})
 def test_no_open(self):
  g,c=self.fixture();g['closed']=False;self.assertRaises(ValueError,self.parse,g,c)
 def test_no_wrong_slug(self):
  g,c=self.fixture();g['slug']='wrong';self.assertRaises(ValueError,self.parse,g,c)
 def test_no_preclose(self):
  g,c=self.fixture();self.assertRaises(ValueError,parse_evidence,S,g,c,1790301900000)
 def test_duplicate_tokens(self):
  g,c=self.fixture();g['clobTokenIds']='["1","1"]';self.assertRaises(ValueError,self.parse,g,c)
 def test_no_guess_fractional(self):
  g,c=self.fixture();g['outcomePrices']='["0.99","0.01"]';self.assertRaises(ValueError,self.parse,g,{})
 def test_identity_conflict(self):
  g,c=self.fixture();c['tokens'][0]['outcome']='Down';self.assertRaises(ValueError,self.parse,g,c)
 def test_backfill_clock(self):
  g,c=self.fixture();a=self.parse(g,c);self.assertIsNone(a['historical_label_available_ms']);self.assertFalse(a['usable_for_historical_asof_training'])
 def test_source_conflict(self):
  g,c=self.fixture();g['outcomePrices']='["0","1"]';self.assertRaises(ValueError,self.parse,g,c)
 def test_no_cash(self):g,c=self.fixture();self.assertFalse(self.parse(g,c)['cash_receipt'])
 def test_allowlist(self):self.assertRaises(ValueError,fetch_public,'https://example.com/private')
if __name__=='__main__':unittest.main()
