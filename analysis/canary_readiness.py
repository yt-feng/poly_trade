"""Reproducible public-quote research, never a live execution service.

One attempt per market per fixed hypothesis. Maker fills are explicitly proxies;
fees are cash-equivalent scenarios and rebates are zero. Missing exits remain in
an attempt ledger with a conservative full-cost loss bound. No future labels
enter CausalSignals. All thresholds are fixed, not fitted to these results.
"""
from __future__ import annotations
import argparse, bisect, csv, gzip, hashlib, json, random, statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from canary_core import CausalSignals,HYPOTHESES,ShadowRisk,number,observed,fee_per_share,official_winner,live_readiness

SHARES=5

def checked_rows(path):
    side=path.with_name(path.name+'.sha256')
    if not side.is_file():raise ValueError('Missing checksum '+str(path))
    if hashlib.sha256(path.read_bytes()).hexdigest()!=side.read_text().split()[0]:raise ValueError('Bad checksum '+str(path))
    with gzip.open(path,'rt',encoding='utf-8') as f:
        for line in f:yield json.loads(line)

def load(root):
    seen={}; files=[];total=0;duplicates=0;conflicts=0
    for p in sorted(root.rglob('snapshots-*.jsonl.gz')):
        count=0
        for row in checked_rows(p):
            if row.get('asset')!='btc':continue
            count+=1;total+=1;key=(row['slug'],row['sample_ms'])
            if key in seen:
                duplicates+=1
                if seen[key]!=row:conflicts+=1
            else:seen[key]=row
        files.append({'path':str(p),'rows':count,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    if conflicts:raise ValueError('Conflicting duplicate timestamps; reconcile before research')
    all_rows=sorted(seen.values(),key=lambda r:r['sample_ms'])
    rows=[r for r in all_rows if (r.get('microstructure') or {}).get('schema_version')==3]
    cov={'total_snapshot_rows':len(all_rows),'v3_rows':len(rows),'v2_rows':len(all_rows)-len(rows),
         'v3_windows':len({r['slug'] for r in rows}),'v3_utc_days':len({datetime.fromtimestamp(r['sample_ms']/1000,timezone.utc).date() for r in rows}),
         'v3_start_ms':rows[0]['sample_ms'] if rows else None,'v3_end_ms':rows[-1]['sample_ms'] if rows else None,
         'duplicate_rows':duplicates,'conflicting_duplicates':conflicts,'files':files,
         'sample_role':'New production v3 only; previous development clips and v2 excluded from new strategy returns.'}
    return rows,cov

def quality_quote(row,side):
    b=row.get(side) or {};ms=row['sample_ms'];bid=number(b.get('bid'));ask=number(b.get('ask'))
    return (row.get('poly_valid') and observed(b,ms,1500) and bid is not None and ask is not None and 0<bid<=ask<1)

def quote_after(rows,times,target,side,slug):
    i=bisect.bisect_left(times,target)
    while i<len(rows) and times[i]-target<=2500:
        row=rows[i];b=row.get(side) or {}
        if row['slug']!=slug:return None
        rec=number(b.get('received_ms'))
        if rec is not None and target<=rec<=times[i]:return i
        i+=1
    return None

def historical_fee(row):
    ms=row['sample_ms'];m=row.get('microstructure')or{};r=m.get('rules')or{};f=r.get('fees')or{}
    if not observed({'received_ms':r.get('metadata_received_ms')},ms,60000):return None
    rate=number(f.get('rate'))
    return rate if f.get('known') and number(f.get('exponent'))==1 and rate is not None and 0<=rate<=1 else None

def replay(rows,features,hypothesis,hold,delay,mode):
    times=[r['sample_ms'] for r in rows]; used=set();out=[]
    for i,f in enumerate(features):
        side_n=f['directions'][hypothesis]
        if not side_n or f['slug'] in used:continue
        used.add(f['slug']);side='up' if side_n>0 else 'down';row=rows[i]
        item={'hypothesis':hypothesis,'mode':mode,'hold_seconds':hold,'latency_seconds':delay,
          'slug':f['slug'],'decision_ms':times[i],'side':side,'shares':SHARES,
          'status':'no_entry_quote','pnl_lower_bound_usd':0.,'fill_is_proxy':True,'decision_signal_cutoff_ms':f['signal_cutoff_ms']}
        target=times[i]+delay*1000;e=quote_after(rows,times,target,side,f['slug'])
        if e is None:out.append(item);continue
        if not quality_quote(rows[e],side):item['status']='entry_invalid';out.append(item);continue
        entry_rate=historical_fee(rows[e])
        if entry_rate is None:item['status']='entry_fee_unknown';out.append(item);continue
        tick=f['tick_size'];entry_price=None;cancel_effective=None
        if mode=='taker':
            b=rows[e][side]
            if (number(b.get('ask_size')) or 0)<SHARES:item['status']='entry_capacity';out.append(item);continue
            if b['ask']>row[side]['ask']+tick+1e-10:
                item['status']='entry_limit_reject';out.append(item);continue
            entry_price=b['ask']
        else:
            limit=row[side]['bid']
            if rows[e][side]['ask']<=limit:
                item['status']='post_only_would_cross';out.append(item);continue
            expiry=target+5000;fill=None
            for k in range(e,len(rows)):
                now=times[k]
                if now>expiry or rows[k]['slug']!=f['slug']:break
                if k>e and times[k]-times[k-1]>2500:item['status']='entry_path_gap';break
                if cancel_effective is not None and now>=cancel_effective:
                    item['status']='cancelled';break
                adverse=features[k].get('spot_return_5s_bps')
                if hypothesis in ('roll_veto','roll_confirm') and cancel_effective is None and (
                    not features[k]['ready'] or (adverse is not None and side_n*adverse < -1)):
                    cancel_effective=now+delay*1000
                    item['cancel_requested_ms']=now;item['cancel_effective_ms']=cancel_effective
                b=rows[k].get(side)or{}
                if quality_quote(rows[k],side) and b['received_ms']>=target and b['ask']<=limit-tick+1e-10 and (number(b.get('ask_size'))or 0)>=SHARES:
                    fill=k;break
            if fill is None:
                if item['status']=='no_entry_quote':item['status']='expired_no_proxy_fill'
                out.append(item);continue
            e=fill;entry_price=limit;entry_rate=historical_fee(rows[e])
            if entry_rate is None:item['status']='entry_fee_unknown';out.append(item);continue
            item['filled_during_cancel_pending']=cancel_effective is not None
        fee_in=fee_per_share(entry_price,entry_rate) if mode=='taker' else 0.
        item.update(entry_ms=times[e],entry_quote_received_ms=rows[e][side]['received_ms'],entry_price=entry_price,
                    entry_fee_rate=entry_rate,entry_fee_per_share=fee_in,entry_delay_ms=times[e]-times[i],
                    status='exit_unvalued',pnl_lower_bound_usd=-SHARES*(entry_price+fee_in))
        x=quote_after(rows,times,times[e]+(hold+delay)*1000,side,f['slug'])
        if x is None:out.append(item);continue
        if any(times[k]-times[k-1]>2500 for k in range(e+1,x+1)):
            item['status']='exit_path_gap';out.append(item);continue
        b=rows[x][side];exit_rate=historical_fee(rows[x])
        if not quality_quote(rows[x],side) or (number(b.get('bid_size'))or 0)<SHARES or exit_rate is None:
            out.append(item);continue
        xp=b['bid'];fee_out=fee_per_share(xp,exit_rate);gross=xp-entry_price;net=gross-fee_in-fee_out
        stressed=max(0,xp-tick);stress=stressed-entry_price-fee_in-fee_per_share(stressed,exit_rate)
        item.update(status='valued',exit_ms=times[x],exit_quote_received_ms=b['received_ms'],exit_price=xp,
          gross_cents_per_share=gross*100,fees_cents_per_share=(fee_in+fee_out)*100,
          net_cents_per_share=net*100,stress_net_cents_per_share=stress*100,
          pnl_usd=SHARES*net,pnl_lower_bound_usd=SHARES*net)
        out.append(item)
    return out

def summarize(attempts):
    grouped=defaultdict(list)
    for a in attempts:grouped[(a['hypothesis'],a['mode'],a['hold_seconds'],a['latency_seconds'])].append(a)
    result=[]
    for key,g in grouped.items():
        v=[a for a in g if a['status']=='valued'];entered=[a for a in g if 'entry_ms' in a]
        net=[a['net_cents_per_share']for a in v];mean=statistics.mean(net)if net else None;lower=None
        if len(v)>=30:
            rng=random.Random(20260923);means=sorted(statistics.mean(rng.choices(net,k=len(net)))for _ in range(2000));lower=means[50]
        result.append(dict(hypothesis=key[0],mode=key[1],hold_seconds=key[2],latency_seconds=key[3],
          attempts=len(g),proxy_fills=len(entered),valued=len(v),unvalued_fills=len(entered)-len(v),
          valued_exit_fraction=len(v)/len(entered)if entered else 0,mean_net_cents=mean,
          mean_gross_cents=statistics.mean(a['gross_cents_per_share']for a in v)if v else None,
          mean_fee_cents=statistics.mean(a['fees_cents_per_share']for a in v)if v else None,
          stress_mean_net_cents=statistics.mean(a['stress_net_cents_per_share']for a in v)if v else None,
          net_lower95_cents=lower,total_valued_pnl_usd=sum(a['pnl_usd']for a in v),
          total_pnl_lower_bound_usd=sum(a['pnl_lower_bound_usd']for a in g),
          mean_net_without_best_cents=statistics.mean(sorted(net)[:-1])if len(net)>1 else None,
          median_net_cents=statistics.median(net)if net else None,
          wins=sum(x>0 for x in net),independent_windows=len({a['slug']for a in g}),
          statuses=dict(Counter(a['status']for a in g))))
    return sorted(result,key=lambda x:(x['hypothesis'],x['mode'],x['hold_seconds'],x['latency_seconds']))

def mechanism(rows,features):
    """Nonoverlapping ten-second probes; summarize by independent market, not ticks."""
    out=[];times=[r['sample_ms']for r in rows];next_allowed=0
    for i,f in enumerate(features):
        if times[i]<next_allowed or f['roll_pressure_usd_s'] is None:continue
        now=(rows[i].get('microstructure')or{}).get('twap60')or{}
        if not observed(now,times[i],5000,True):continue
        x=bisect.bisect_left(times,times[i]+5000)
        if x>=len(rows)or times[x]-times[i]>6500 or rows[x]['slug']!=rows[i]['slug']:continue
        if any(times[k]-times[k-1]>2500 for k in range(i+1,x+1)):continue
        later=(rows[x].get('microstructure')or{}).get('twap60')or{}
        if not observed(later,times[x],5000,True):continue
        n=number(now.get('price'));l=number(later.get('price'))
        if n is None or l is None:continue
        actual=l-n;roll=f['roll_pressure_usd_s']*5;trend=f['twap_change_5s']
        out.append({'slug':f['slug'],'decision_ms':times[i],'label_available_ms':times[x],
          'actual_twap_change_usd':actual,'roll_prediction_usd':roll,'trend_prediction_usd':trend,
          'roll_absolute_error':abs(roll-actual),'trend_absolute_error':abs(trend-actual)if trend is not None else None})
        next_allowed=times[i]+10000
    return out

def write_csv(path,rows):
    if not rows:path.write_text('');return
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='',encoding='utf-8-sig')as f:
        w=csv.DictWriter(f,fields);w.writeheader()
        for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False)if isinstance(v,(list,dict))else v for k,v in r.items()})

def run(root,out):
    out.mkdir(parents=True,exist_ok=True);rows,cov=load(root)
    if not rows:raise ValueError('No production v3. Do not replace with old smoke data.')
    engine=CausalSignals();features=[engine.update(r)for r in rows]
    attempts=[]
    for name in HYPOTHESES:
        for mode in ('taker','maker_trade_through_proxy'):
            for hold in (15,30):
                for delay in (1,3):attempts+=replay(rows,features,name,hold,delay,mode)
    summaries=summarize(attempts)
    present={(s['hypothesis'],s['mode'],s['hold_seconds'],s['latency_seconds'])for s in summaries}
    for name in HYPOTHESES:
        for mode in ('taker','maker_trade_through_proxy'):
            for hold in (15,30):
                for delay in (1,3):
                    if(name,mode,hold,delay)not in present:summaries.append(dict(hypothesis=name,mode=mode,hold_seconds=hold,latency_seconds=delay,attempts=0,proxy_fills=0,valued=0,mean_net_cents=None))
    for s in summaries:s['readiness']=live_readiness(s,cov)
    labels=[];conflicts=[];market_rows={r['slug']:r for r in rows};by_slug={}
    for p in sorted(root.rglob('labels-*.jsonl.gz')):
        for label in checked_rows(p):
            if label.get('kind')!='market_resolved':continue
            for slug,r in market_rows.items():
                v=official_winner(label,r)
                if v:
                    if slug in by_slug and by_slug[slug]['winner']!=v['winner']:conflicts.append(slug)
                    else:by_slug[slug]=v
    labels=[v for k,v in by_slug.items()if k not in conflicts]
    cov.update(official_label_windows=len(labels),label_conflicts=conflicts,
      official_threshold_valid_rows=sum(bool(r.get('official_threshold_valid'))for r in rows),
      valid_rule_matched_reference_rows=sum(bool(r.get('matching_reference_feed_valid'))for r in rows),
      decision_ready_rows=sum(f['ready']for f in features),blocked_reasons=dict(Counter(k for f in features for k in f['block_reasons'])))
    lock_path=Path(__file__).resolve().parents[1]/'config/canary_shortlist_lock.json'
    lock=json.loads(lock_path.read_text())
    cutoff=lock['forward_min_window_start_ms']
    forward_attempts=[a for a in attempts if int(a['slug'].rsplit('-',1)[1])*1000>=cutoff]
    forward_rows=[r for r in rows if int(r['slug'].rsplit('-',1)[1])*1000>=cutoff]
    forward_summaries=summarize(forward_attempts)
    shortlist=[s for s in forward_summaries if s['hypothesis']==lock['candidate'] and s['mode']==lock['mode'] and s['hold_seconds']==lock['hold_seconds']]
    forward={'lock':lock,'rows':len(forward_rows),'independent_windows':len({r['slug'] for r in forward_rows}),
      'previously_seen_windows_excluded':True,'shortlist_results':shortlist,'live_eligible':False,
      'note':'Subsequent windows after a post-hoc shortlist, not an independently sized confirmatory trial.'}
    (out/'shortlist_forward.json').write_text(json.dumps(forward,indent=2))
    write_csv(out/'forward_configurations.csv',forward_summaries)
    probes=mechanism(rows,features);aggregate=[]
    for slug in sorted({p['slug']for p in probes}):
        g=[p for p in probes if p['slug']==slug and p['trend_absolute_error']is not None]
        if g:aggregate.append({'slug':slug,'nonoverlapping_probes':len(g),'roll_mae':statistics.mean(p['roll_absolute_error']for p in g),'trend_mae':statistics.mean(p['trend_absolute_error']for p in g)})
    primary=[a for a in attempts if a['hypothesis']=='roll_veto'and a['mode']=='maker_trade_through_proxy'and a['hold_seconds']==15 and a['latency_seconds']==1]
    risk=ShadowRisk();risk_log=[]
    for a in primary:
        if 'entry_ms'not in a:continue
        status=risk.intent({'ready':True,'slug':a['slug']},SHARES*a['entry_price'])
        if status=='virtual_intent':risk.close(a.get('pnl_usd'))
        risk_log.append({'slug':a['slug'],'state':status,'virtual_pnl':risk.pnl,'halted':risk.halted})
    report={'scope':'shadow-only research; no orders, no live adapter','coverage':cov,'configurations':summaries,
       'forward_shortlist':forward,
       'primary':'Initial predeclared primary: roll_veto / maker trade-through proxy / 15s / 1s','live_canary_eligible':False,
       'virtual_primary_risk_trace':risk_log,'mechanism_by_market':aggregate,
       'cautions':['Only production v3 used for fresh returns','Maker fills are crossed-quote proxies, not actual fills',
       'Fees use observed rule parameters but cash-equivalent model; no rebates',
       'All missing exits retained with full-cost lower bound','No independent precommitted holdout yet',
       'Original trained models are not refitted or safe to deploy; legacy live files are unchanged']}
    for name,value in [('readiness.json',report),('coverage.json',cov),('official_labels.json',labels)]:
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
    write_csv(out/'attempts.csv',attempts);write_csv(out/'configurations.csv',summaries)
    write_csv(out/'mechanism_probes.csv',probes);write_csv(out/'mechanism_by_market.csv',aggregate)
    with(out/'shadow_decisions.jsonl').open('w')as f:
        for x in features:f.write(json.dumps(x,ensure_ascii=False,allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in cov.items()if k!='files'},indent=2))
    print('LIVE_CANARY_ELIGIBLE=false; configurations='+str(len(summaries)))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.input,a.output)
