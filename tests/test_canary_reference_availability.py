"""Synthetic public-data availability contracts; never submit an order."""
from pathlib import Path
from copy import deepcopy
import hashlib,json,sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from reference_availability_audit import (evaluate_metadata, identity, positive,
    context_observation, book_observation, summarize, PublicFetcher, verify_directory)

SLUG='btc-updown-5m-1790306700'
CID='0x'+'a'*64
START=1790306700000

def fixtures(value='86000'):
    m=dict(slug=SLUG,conditionId=CID,clobTokenIds='["123","456"]',outcomes='["Up","Down"]',
           active=True,closed=False,acceptingOrders=True,events=[])
    e=dict(slug=SLUG,markets=[deepcopy(m)],eventMetadata={} if value is None else dict(priceToBeat=value))
    return m,e

class ReferenceTests(unittest.TestCase):
    def test_full_event_open(self):
        m,e=fixtures();r=evaluate_metadata(SLUG,m,e,START+30000)
        self.assertTrue(r['threshold_observed_before_close']);self.assertEqual(r['published_price_to_beat'],'86000')
    def test_compact_market_event(self):
        m,e=fixtures();m['events']=[dict(slug=SLUG,eventMetadata=dict(priceToBeat='86000'))]
        self.assertTrue(evaluate_metadata(SLUG,m,None,START+1)['threshold_observed_before_close'])
    def test_post_close_not_asof(self):
        m,e=fixtures();r=evaluate_metadata(SLUG,m,e,START+300000)
        self.assertFalse(r['threshold_observed_before_close']);self.assertIsNone(r['historical_first_availability_ms'])
    def test_before_start(self):
        m,e=fixtures();self.assertFalse(evaluate_metadata(SLUG,m,e,START-1)['threshold_observed_before_close'])
    def test_late_response_not_request_time(self):
        m,e=fixtures();self.assertEqual(evaluate_metadata(SLUG,m,e,START+300001)['reason'],'POST_CLOSE_REFERENCE_ONLY')
    def test_absent_not_inferred_from_price(self):
        m,e=fixtures(None);m['lastTradePrice']='.99';r=evaluate_metadata(SLUG,m,e,START+1000)
        self.assertIsNone(r['published_price_to_beat']);self.assertEqual(r['reason'],'PRICE_TO_BEAT_ABSENT')
    def test_wrong_event(self):
        m,e=fixtures();e['slug']='other';self.assertFalse(evaluate_metadata(SLUG,m,e,START+1)['threshold_observed_before_close'])
    def test_wrong_condition(self):
        m,e=fixtures();e['markets'][0]['conditionId']='0x'+'b'*64
        self.assertIn('EVENT_MARKET_IDENTITY_CONFLICT',evaluate_metadata(SLUG,m,e,START+1)['errors'])
    def test_opposite_token_mapping(self):
        m,e=fixtures();e['markets'][0]['clobTokenIds']='["456","123"]'
        self.assertFalse(evaluate_metadata(SLUG,m,e,START+1)['threshold_observed_before_close'])
    def test_conflicting_sources(self):
        m,e=fixtures();m['events']=[dict(slug=SLUG,eventMetadata=dict(priceToBeat='86001'))]
        r=evaluate_metadata(SLUG,m,e,START+1);self.assertIsNone(r['published_price_to_beat'])
    def test_decimal_format_not_conflict(self):
        m,e=fixtures();m['events']=[dict(slug=SLUG,eventMetadata=dict(priceToBeat='86000.000'))]
        self.assertTrue(evaluate_metadata(SLUG,m,e,START+1)['threshold_observed_before_close'])
    def test_final_present_blocks_open_use(self):
        m,e=fixtures();e['eventMetadata']['finalPrice']='86100'
        self.assertFalse(evaluate_metadata(SLUG,m,e,START+1)['threshold_observed_before_close'])
    def test_flags_required(self):
        for name in ('closed','active','acceptingOrders'):
            m,e=fixtures();m.pop(name);self.assertFalse(evaluate_metadata(SLUG,m,e,START+1)['threshold_observed_before_close'])
    def test_invalid_threshold(self):
        for v in (True,'NaN','Infinity','0','-3'):
            m,e=fixtures(v);self.assertIsNone(evaluate_metadata(SLUG,m,e,START+1)['published_price_to_beat'])
    def test_bad_market_identity_raises(self):
        m,e=fixtures();m['clobTokenIds']='["123","123"]';self.assertRaises(ValueError,identity,m,SLUG)
    def test_itode_bool_only(self):
        self.assertIsNone(context_observation({'itode':'true'},CID)['itode'])
        self.assertTrue(context_observation({'itode':True},CID)['itode'])
    def test_itode_wrong_condition(self):
        self.assertIsNone(context_observation({'itode':True,'condition_id':'wrong'},CID)['itode'])
    def test_order_constraints_token_binding(self):
        p=dict(asset_id='123',market=CID,min_order_size='5',tick_size='.01')
        self.assertEqual(book_observation(p,'123',CID)['min_order_size'],'5')
        self.assertIsNone(book_observation(p,'456',CID)['min_order_size'])
    def test_summary_distinguishes_post_close(self):
        m,e=fixtures();a=evaluate_metadata(SLUG,m,e,START+300001)
        s=summarize([a],[]);self.assertEqual(s['open_threshold_observations'],0)
        self.assertFalse(s['trading_enabled']);self.assertFalse(s['account_cash_verified'])
    def test_endpoint_allowlist(self):
        self.assertRaises(ValueError,PublicFetcher().get,'https://clob.polymarket.com/order')
        self.assertRaises(ValueError,PublicFetcher().get,'https://evil.example/markets')
    def test_input_not_mutated(self):
        m,e=fixtures();before=deepcopy((m,e));evaluate_metadata(SLUG,m,e,START+1);self.assertEqual((m,e),before)
    def test_saved_response_hash_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);b='{}';r=dict(body_utf8=b,sha256=hashlib.sha256(b.encode()).hexdigest())
            f=root/'http_responses.jsonl';f.write_text(json.dumps(r)+'\n')
            (root/'manifest.json').write_text(json.dumps(dict(files={f.name:hashlib.sha256(f.read_bytes()).hexdigest()})))
            self.assertEqual(verify_directory(root)['verified_response_bodies'],1)
            f.write_text('{}\n');self.assertRaises(ValueError,verify_directory,root)

if __name__=='__main__':unittest.main()
