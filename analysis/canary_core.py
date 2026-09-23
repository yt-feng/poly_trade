"""Causal, offline-only signal and readiness primitives. No order API or secrets."""
from __future__ import annotations
from collections import deque
from decimal import Decimal
import math

HYPOTHESES=('roll','roll_veto','roll_confirm','roll_conflict','twap_trend')

def number(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError): return None

def sign(x): return 1 if x>0 else -1 if x<0 else 0

def observed(obj, now, age=5000, event=False):
    if not isinstance(obj,dict): return False
    receipt=number(obj.get('received_ms'))
    if receipt is None or not 0<=now-receipt<=age: return False
    stamp=number(obj.get('event_ms'))
    return not event or (stamp is not None and 0<=now-stamp<=age)

def price(obj): return number((obj or {}).get('price'))

def fee_per_share(p, rate, exponent=1):
    if not (0<=p<=1 and 0<=rate<=1 and exponent==1):
        raise ValueError('Unknown fee model or invalid price/rate')
    return float(Decimal(str(rate))*Decimal(str(p))*(1-Decimal(str(p))))

def official_winner(label, row):
    """Only an observed market_resolved message for this exact market is a label."""
    if label.get('kind')!='market_resolved': return None
    p=label.get('payload') or {}
    if p.get('event_type')!='market_resolved': return None
    ms=number(label.get('available_at_ms'))
    end=int(row['slug'].rsplit('-',1)[1])*1000+300000
    if ms is None or ms<end: return None
    rules=(row.get('microstructure') or {}).get('rules') or {}
    if not rules.get('condition_id') or p.get('market')!=rules['condition_id']:return None
    up,down=row.get('up_token_id'),row.get('down_token_id')
    if not up or not down or set(p.get('assets_ids',[]))!={up,down}:return None
    winner=p.get('winning_asset_id')
    if winner not in (up,down):return None
    result='up' if winner==up else 'down'
    if str(p.get('winning_outcome','')).lower()!=result:return None
    return {'slug':row['slug'],'winner':result,'label_available_at_ms':int(ms),
            'source':'polymarket_market_resolved','no_proxy_labels':True}

class CausalSignals:
    def __init__(self):self.history=deque();self.last_ms=None
    def update(self,row):
        ms=int(row['sample_ms'])
        if self.last_ms is not None and ms<=self.last_ms:raise ValueError('Strictly increasing sample time required')
        if self.last_ms is not None and ms-self.last_ms>2500:self.history.clear()
        self.last_ms=ms
        m=row.get('microstructure') or {};rules=m.get('rules') or {}
        valid=dict(m.get('valid') or {})
        micro_causal=number(m.get('asof_ms')) is not None and m['asof_ms']<=ms
        cl=row.get('chainlink') or {};spot=row.get('binance') or {}
        cb=m.get('coinbase_ticker') or {};tw=m.get('twap60') or {}
        # Coinbase v3 ticker uses `price` as well as bid/ask.
        cbp=number((cb.get('payload') or {}).get('price'))
        current={'ms':ms,'chainlink':price(cl) if row.get('chainlink_valid') and observed(cl,ms,5000,True) else None,
          'spot':price(spot) if row.get('binance_valid') and observed(spot,ms,5000,True) else None,
          'coinbase':cbp if micro_causal and valid.get('coinbase') and observed(cb,ms,5000) else None,
          'twap':price(tw) if micro_causal and valid.get('twap60') and observed(tw,ms,5000,True) else None}
        past={}
        for horizon in (5,60):
            target=ms-horizon*1000
            match=next((x for x in reversed(self.history) if x['ms']<=target),None)
            past[horizon]=match if match and target-match['ms']<=1500 else None
        self.history.append(current)
        while self.history and self.history[0]['ms']<ms-65000:self.history.popleft()
        def delta(key,h,return_bps=False):
            old=(past[h] or {}).get(key);new=current.get(key)
            if old is None or new is None or old<=0:return None
            return 1e4*(new/old-1) if return_bps else new-old
        move=delta('chainlink',60);spot5=delta('spot',5,True);cb5=delta('coinbase',5,True)
        twap5=delta('twap',5)
        pressure=move/60 if move is not None else None
        flow=m.get('spot_flow') or {}; f5=flow.get('5s') or {}
        flow_ms=number(flow.get('last_trade_received_ms'))
        flow5=number(f5.get('imbalance')) if (micro_causal and flow.get('connected') and f5.get('warmed') and
             not f5.get('recent_id_gap_or_overflow') and flow_ms is not None and 0<=ms-flow_ms<=5000) else None
        failures=[]
        if not m or not micro_causal:failures.append('missing_or_future_v3')
        if not valid.get('rules') or not observed({'received_ms':rules.get('metadata_received_ms')},ms,60000):failures.append('rules_stale')
        if rules.get('reference_kind')!='chainlink_twap_60':failures.append('wrong_reference_rule')
        if current['twap'] is None:failures.append('twap_unavailable')
        if current['spot'] is None:failures.append('spot_unavailable')
        if not rules.get('active') or rules.get('closed') or not rules.get('accepting_orders'):failures.append('market_not_open')
        fees=rules.get('fees') or {};rate=number(fees.get('rate'));exp=number(fees.get('exponent'))
        if not fees.get('known') or rate is None or not 0<=rate<=1 or exp!=1:failures.append('fee_unknown')
        if not (fees.get('schedule') or {}).get('takerOnly'):failures.append('maker_fee_unknown')
        if number(rules.get('min_order_size')) is None or rules['min_order_size']>5:failures.append('minimum_order')
        tick=number(rules.get('tick_size'))
        if tick is None or not 0<tick<1:failures.append('tick_unknown')
        books=[row.get(s) or {} for s in ('up','down')]
        if not row.get('poly_valid') or not all(observed(b,ms,1500) for b in books):failures.append('book_stale')
        receipts=[number(b.get('received_ms')) for b in books]
        if None in receipts or abs(receipts[0]-receipts[1])>250:failures.append('book_skew')
        for b in books:
            bid,ask=number(b.get('bid')),number(b.get('ask'))
            if bid is None or ask is None or not 0<bid<=ask<1 or ask-bid>.03000001:failures.append('book_price')
        if number(row.get('sampler_lag_ms')) is None or row['sampler_lag_ms']>100:failures.append('sampling_lag')
        start=int(row['slug'].rsplit('-',1)[1])*1000
        ttl=(start+300000-ms)/1000
        if not 50<=ttl<=285:failures.append('entry_window')
        directions={name:0 for name in HYPOTHESES}
        ready=not failures
        if ready:
            if pressure is not None and abs(pressure)>=.1 and spot5 is not None:
                side=sign(pressure);directions['roll']=side
                adverse=side*spot5 < -1
                if not adverse:directions['roll_veto']=side
                if not adverse and cb5 is not None and side*spot5>=.5 and side*cb5>=.5 and flow5 is not None and side*flow5>=.2:
                    directions['roll_confirm']=side
                if adverse:directions['roll_conflict']=side
            if twap5 is not None and abs(twap5)>=.5:directions['twap_trend']=sign(twap5)
        return {'sample_ms':ms,'slug':row['slug'],'signal_cutoff_ms':ms,'ready':ready,
          'block_reasons':sorted(set(failures)),'roll_pressure_usd_s':pressure,'spot_return_5s_bps':spot5,
          'coinbase_return_5s_bps':cb5,'spot_flow_5s':flow5,'twap_change_5s':twap5,
          'directions':directions,'fee_rate':rate,'tick_size':tick,'ttl_seconds':ttl,
          'official_threshold_valid':bool(row.get('official_threshold_valid')),
          'source':'observed_prefix_only','execution_enabled':False}

def live_readiness(summary, coverage):
    """Research requirements are policy choices, not a statistical guarantee."""
    reasons=[]
    if coverage['v3_windows']<300:reasons.append('fewer_than_300_independent_windows')
    if coverage['v3_utc_days']<7:reasons.append('fewer_than_7_utc_days')
    if summary.get('valued',0)<100:reasons.append('fewer_than_100_filled_attempts')
    if summary.get('valued_exit_fraction',0)<.99:reasons.append('unvalued_exits')
    if summary.get('net_lower95_cents') is None or summary['net_lower95_cents']<=0:reasons.append('nonpositive_or_unavailable_lower_bound')
    if summary.get('stress_mean_net_cents') is None or summary['stress_mean_net_cents']<=0:reasons.append('fails_one_tick_stress')
    reasons.extend(['no_independent_precommitted_holdout','no_private_fill_cancel_reconciliation'])
    return {'eligible_for_live_canary':False,'live_enabled':False,'reasons':reasons,
      'scope':'No automatic live activation; this package has no order API.'}

class ShadowRisk:
    """A small virtual risk ledger. Does not represent an account or send orders."""
    def __init__(self):self.used=set();self.position=None;self.pnl=0.;self.halted=False
    def intent(self,feature,notional):
        if self.halted:return 'halted'
        if not feature.get('ready'):return 'data_blocked'
        if self.position is not None:return 'one_position_limit'
        if feature['slug'] in self.used:return 'duplicate_market'
        if not 0<notional<=5:return 'notional_cap'
        self.used.add(feature['slug']);self.position=feature['slug'];return 'virtual_intent'
    def close(self,pnl):
        if self.position is None:raise ValueError('No virtual position')
        self.position=None
        if number(pnl) is None:self.halted=True;return
        self.pnl+=pnl
        if self.pnl<=-2:self.halted=True
