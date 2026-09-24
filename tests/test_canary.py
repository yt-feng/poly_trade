import copy,gzip,hashlib,json,math,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from canary_core import *
from canary_readiness import replay,quote_after,summarize,checked_rows

START=1790142000000

def fixture(i):
    ms=START+i*1000
    ob={'price':100+i*.2,'received_ms':ms-20,'event_ms':ms-50}
    book={'received_ms':ms-10,'event_ms':ms-20,'bid':.49,'ask':.50,'bid_size':100,'ask_size':100}
    rules={'slug':f'btc-updown-5m-{START//1000}','condition_id':'condition','metadata_received_ms':ms-50,
        'reference_kind':'chainlink_twap_60','active':True,'closed':False,'accepting_orders':True,
        'min_order_size':5,'tick_size':.01,'fees':{'known':True,'rate':.07,'exponent':1,'schedule':{'takerOnly':True}}}
    return {'sample_ms':ms,'asset':'btc','slug':rules['slug'],'sampler_lag_ms':1,
      'up_token_id':'up-token','down_token_id':'down-token','poly_valid':True,'binance_valid':True,'chainlink_valid':True,
      'up':copy.deepcopy(book),'down':copy.deepcopy(book),'binance':copy.deepcopy(ob),'chainlink':copy.deepcopy(ob),
      'microstructure':{'schema_version':3,'asof_ms':ms,'rules':rules,'twap60':copy.deepcopy(ob),
        'coinbase_ticker':{'payload':{'price':ob['price']},'received_ms':ms-10},
        'spot_flow':{'connected':True,'last_trade_received_ms':ms-10,'5s':{'warmed':True,'recent_id_gap_or_overflow':False,'imbalance':.9}},
        'valid':{'rules':True,'coinbase':True,'twap60':True}}}

def fake_features(rows,side=1,name='roll'):
    return [dict(sample_ms=r['sample_ms'],slug=r['slug'],directions={k:side if k==name else 0 for k in HYPOTHESES},
                 signal_cutoff_ms=r['sample_ms'],tick_size=.01,ready=True,spot_return_5s_bps=0)for r in rows]

class Causality(unittest.TestCase):
    def test_future_mutation(self):
        rows=[fixture(i)for i in range(180)];before=copy.deepcopy(rows)
        for r in rows[91:]:r['chainlink']['price']+=1000;r['binance']['price']+=1000
        a=CausalSignals();b=CausalSignals()
        x=[a.update(r)for r in before];y=[b.update(r)for r in rows]
        self.assertEqual(x[:91],y[:91]);self.assertNotEqual(x[-1],y[-1])
    def test_prefix_replay(self):
        rows=[fixture(i)for i in range(180)];a=CausalSignals();full=[a.update(r)for r in rows]
        b=CausalSignals();prefix=[b.update(r)for r in rows[:91]];self.assertEqual(full[:91],prefix)
    def test_final_price_never_enters_causal_signal(self):
        clean=[fixture(i)for i in range(180)]
        leaked=copy.deepcopy(clean)
        for i,r in enumerate(leaked):
            r['final_price']=10**9 if i%2 else -10**9
            r['target_price']=-10**9 if i%3 else 10**9
            r['outcome_up']=bool(i%2)
        a=CausalSignals();b=CausalSignals()
        self.assertEqual([a.update(r)for r in clean],[b.update(r)for r in leaked])
    def test_future_receipt(self):
        r=fixture(80);r['chainlink']['received_ms']=r['sample_ms']+1
        e=CausalSignals();e.update(r);self.assertIsNone(e.history[-1]['chainlink'])
    def test_future_source_event(self):
        r=fixture(80);r['chainlink']['event_ms']=r['sample_ms']+1
        e=CausalSignals();e.update(r);self.assertIsNone(e.history[-1]['chainlink'])
    def test_future_micro(self):
        r=fixture(80);r['microstructure']['asof_ms']+=1;self.assertFalse(CausalSignals().update(r)['ready'])
    def test_gap_resets(self):
        e=CausalSignals()
        for i in range(70):e.update(fixture(i))
        self.assertIsNone(e.update(fixture(90))['roll_pressure_usd_s'])
    def test_duplicate_rejected(self):
        e=CausalSignals();e.update(fixture(80))
        with self.assertRaises(ValueError):e.update(fixture(80))
    def test_wrong_rule(self):
        r=fixture(80);r['microstructure']['rules']['reference_kind']='spot'
        self.assertIn('wrong_reference_rule',CausalSignals().update(r)['block_reasons'])
    def test_minimum_size(self):
        r=fixture(80);r['microstructure']['rules']['min_order_size']=10
        self.assertIn('minimum_order',CausalSignals().update(r)['block_reasons'])
    def test_rule_age(self):
        r=fixture(80);r['microstructure']['rules']['metadata_received_ms']-=70000
        self.assertFalse(CausalSignals().update(r)['ready'])
    def test_unknown_fee(self):
        r=fixture(80);r['microstructure']['rules']['fees']['known']=False
        self.assertFalse(CausalSignals().update(r)['ready'])
    def test_closed_market(self):
        r=fixture(80);r['microstructure']['rules']['closed']=True
        self.assertFalse(CausalSignals().update(r)['ready'])
    def test_crossed_book(self):
        r=fixture(80);r['up']['bid']=.51;self.assertFalse(CausalSignals().update(r)['ready'])
    def test_book_skew(self):
        r=fixture(80);r['up']['received_ms']-=300;self.assertFalse(CausalSignals().update(r)['ready'])
    def test_nan_price(self):
        r=fixture(80);r['binance']['price']=float('nan');self.assertFalse(CausalSignals().update(r)['ready'])
    def test_roll_arithmetic(self):
        e=CausalSignals()
        for i in range(100):v=e.update(fixture(i))
        self.assertAlmostEqual(v['roll_pressure_usd_s'],.2);self.assertEqual(v['directions']['roll'],1)
    def test_never_live(self):
        self.assertFalse(CausalSignals().update(fixture(80))['execution_enabled'])

class Labels(unittest.TestCase):
    def valid(self):return {'kind':'market_resolved','available_at_ms':START+301000,'payload':{'event_type':'market_resolved',
      'market':'condition','assets_ids':['up-token','down-token'],'winning_asset_id':'up-token','winning_outcome':'Up'}}
    def test_official(self):self.assertEqual(official_winner(self.valid(),fixture(80))['winner'],'up')
    def test_no_proxy(self):self.assertIsNone(official_winner({'kind':'spot','final_price':200,'target_price':100},fixture(80)))
    def test_wrong_market(self):
        x=self.valid();x['payload']['market']='other';self.assertIsNone(official_winner(x,fixture(80)))
    def test_wrong_token(self):
        x=self.valid();x['payload']['winning_asset_id']='other';self.assertIsNone(official_winner(x,fixture(80)))
    def test_before_close(self):
        x=self.valid();x['available_at_ms']=START+200000;self.assertIsNone(official_winner(x,fixture(80)))
    def test_name_conflict(self):
        x=self.valid();x['payload']['winning_outcome']='Down';self.assertIsNone(official_winner(x,fixture(80)))

class Execution(unittest.TestCase):
    def test_new_quote_only(self):
        rows=[fixture(i)for i in range(10)];times=[r['sample_ms']for r in rows]
        self.assertEqual(quote_after(rows,times,START+1000,'up',rows[0]['slug']),2)
    def test_one_attempt(self):
        rows=[fixture(i)for i in range(80)];out=replay(rows,fake_features(rows),'roll',15,1,'taker');self.assertEqual(len(out),1)
    def test_entry_limit_reject(self):
        rows=[fixture(i)for i in range(80)];rows[2]['up']['ask']=.53
        self.assertEqual(replay(rows,fake_features(rows),'roll',15,1,'taker')[0]['status'],'entry_limit_reject')
    def test_missing_exit_retained(self):
        rows=[fixture(i)for i in range(10)];a=replay(rows,fake_features(rows),'roll',15,1,'taker')[0]
        self.assertEqual(a['status'],'exit_unvalued');self.assertLess(a['pnl_lower_bound_usd'],0)
    def test_no_fake_maker_fill(self):
        rows=[fixture(i)for i in range(40)];a=replay(rows,fake_features(rows),'roll',15,1,'maker_trade_through_proxy')[0]
        self.assertNotIn('entry_ms',a)
    def test_postonly_reject(self):
        rows=[fixture(i)for i in range(40)];rows[2]['up'].update(bid=.48,ask=.49)
        a=replay(rows,fake_features(rows),'roll',15,1,'maker_trade_through_proxy')[0]
        self.assertEqual(a['status'],'post_only_would_cross')
    def test_cancel_pending_can_fill(self):
        rows=[fixture(i)for i in range(40)];f=fake_features(rows,name='roll_veto')
        f[2]['spot_return_5s_bps']=-2;rows[2]['up'].update(bid=.47,ask=.50)
        rows[3]['up'].update(bid=.47,ask=.48)
        a=replay(rows,f,'roll_veto',15,3,'maker_trade_through_proxy')[0]
        f[4]['spot_return_5s_bps']=-2;rows[5]['up'].update(bid=.47,ask=.48)
        a=replay(rows,f,'roll_veto',15,3,'maker_trade_through_proxy')[0]
        self.assertTrue(a.get('filled_during_cancel_pending'));self.assertLess(a['entry_ms'],a['cancel_effective_ms'])
    def test_no_cross_window_exit(self):
        rows=[fixture(i)for i in range(40)]
        for r in rows[10:]:r['slug']='btc-updown-5m-1790142300'
        out=replay(rows,fake_features(rows),'roll',15,1,'taker');self.assertEqual(out[0]['status'],'exit_unvalued')
    def test_fee_symmetry(self):self.assertAlmostEqual(fee_per_share(.3,.07),fee_per_share(.7,.07))
    def test_fee_invalid(self):
        with self.assertRaises(ValueError):fee_per_share(.5,.07,2)
    def test_small_n_no_ci(self):
        rows=[fixture(i)for i in range(80)];a=replay(rows,fake_features(rows),'roll',15,1,'taker')
        self.assertIsNone(summarize(a)[0]['net_lower95_cents'])

class Controls(unittest.TestCase):
    def test_risk_cap(self):self.assertEqual(ShadowRisk().intent({'ready':True,'slug':'a'},6),'notional_cap')
    def test_single_position(self):
        r=ShadowRisk();r.intent({'ready':True,'slug':'a'},3);self.assertEqual(r.intent({'ready':True,'slug':'b'},3),'one_position_limit')
    def test_loss_halt(self):
        r=ShadowRisk();r.intent({'ready':True,'slug':'a'},3);r.close(-2.01);self.assertTrue(r.halted)
    def test_unknown_pnl_halt(self):
        r=ShadowRisk();r.intent({'ready':True,'slug':'a'},3);r.close(None);self.assertTrue(r.halted)
    def test_live_gate(self):self.assertFalse(live_readiness({}, {'v3_windows':4,'v3_utc_days':1})['eligible_for_live_canary'])
    def test_checksum_rejected(self):
        with tempfile.TemporaryDirectory()as d:
            p=Path(d)/'s.gz';p.write_bytes(gzip.compress(b'{}\n'));p.with_name(p.name+'.sha256').write_text('0'*64)
            with self.assertRaises(ValueError):list(checked_rows(p))

if __name__=='__main__':unittest.main()
