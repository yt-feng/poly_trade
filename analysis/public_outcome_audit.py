"""Bounded PUBLIC outcome evidence backfill. No wallets, orders or strategies.

Every response is stored with retrieval time and SHA256. Backfilled outcomes
must never be relabelled as if observed at the historical market close.
"""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, json, re, time, threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

GAMMA='https://gamma-api.polymarket.com/markets/slug/'
CLOB='https://clob.polymarket.com/markets/'
SLUG=re.compile(r'btc-updown-5m-(\d{10})$')
CID=re.compile(r'0x[0-9a-fA-F]{64}$')

class AccessDenied(RuntimeError): pass
STOP=threading.Event()

def fetch_public(url):
    if not (url.startswith(GAMMA) or url.startswith(CLOB)):
        raise ValueError('Unexpected public resource')
    for attempt in range(3):
        if STOP.is_set(): raise AccessDenied('Collection halted after access restriction')
        try:
            with urlopen(Request(url,headers={'User-Agent':'public-outcome-evidence/1.0'}),timeout=12) as r:
                if urlparse(r.url).hostname!=urlparse(url).hostname:
                    raise ValueError('Unexpected redirect')
                body=r.read(2*1024*1024+1)
                if len(body)>2*1024*1024: raise ValueError('Response byte cap')
            return json.loads(body),hashlib.sha256(body).hexdigest(),int(time.time()*1000),body.decode('utf-8')
        except HTTPError as e:
            if e.code in (401,403,418,451):
                STOP.set(); raise AccessDenied(str(e))
            if e.code==429:
                retry=e.headers.get('Retry-After','60')
                wait=int(retry) if retry.isdigit() else 60
                if wait>60: raise RuntimeError('Server cooldown exceeds bounded run')
                time.sleep(max(wait,1))
            elif e.code>=500 and attempt<2: time.sleep(2**attempt)
            else: raise
    raise RuntimeError('Public request retry budget exhausted')

def array(x):
    return json.loads(x) if isinstance(x,str) else x

def parse_evidence(slug,gamma,clob,received_ms):
    """Require explicit finality plus exact identifiers; prices alone never win."""
    m=SLUG.fullmatch(slug)
    if not m or gamma.get('slug')!=slug: raise ValueError('SLUG_IDENTITY')
    end=(int(m[1])+300)*1000
    if received_ms<end: raise ValueError('NOT_ENDED')
    cid=gamma.get('conditionId')
    if not cid or not CID.fullmatch(cid): raise ValueError('CONDITION_ID')
    ids=array(gamma.get('clobTokenIds',[])); names=array(gamma.get('outcomes',[]))
    if len(ids)!=2 or len(names)!=2 or set(str(n).lower()for n in names)!={'up','down'}:
        raise ValueError('TOKEN_MAPPING')
    mapping={str(i):str(n).lower() for i,n in zip(ids,names)}
    if len(mapping)!=2: raise ValueError('DUPLICATE_TOKEN')
    if gamma.get('closed') is not True: raise ValueError('NOT_CLOSED')
    tokens=clob.get('tokens',[]) if isinstance(clob,dict) else []
    winner=None; method=None
    if (clob.get('condition_id')==cid and clob.get('closed') is True
        and len(tokens)==2 and {str(t.get('token_id'))for t in tokens}==set(mapping)):
        if any(str(t.get('outcome','')).lower()!=mapping[str(t['token_id'])] for t in tokens):
            raise ValueError('CLOB_OUTCOME_IDENTITY')
        winners=[t for t in tokens if t.get('winner') is True]
        if len(winners)==1 and all(type(t.get('winner'))is bool for t in tokens):
            winner=mapping[str(winners[0]['token_id'])]; method='clob_explicit_winner'
    status=str(gamma.get('umaResolutionStatus','')).lower()
    prices=array(gamma.get('outcomePrices',[]))
    if winner is not None and status=='resolved' and len(prices)==2 and set(map(str,prices))=={'0','1'}:
        other=str(names[list(map(str,prices)).index('1')]).lower()
        if other!=winner: raise ValueError('FINAL_OUTCOME_SOURCE_CONFLICT')
    if winner is None and status=='resolved' and len(prices)==2:
        ps=[str(x)for x in prices]
        if set(ps)=={'0','1'}:
            winner=str(names[ps.index('1')]).lower();method='gamma_resolved_exact_payout'
    if winner is None: raise ValueError('NO_EXPLICIT_FINAL_OUTCOME')
    return dict(slug=slug,condition_id=cid,tokens=mapping,winner=winner,
                source=method,label_observed_ms=received_ms,market_end_ms=end,
                historical_label_available_ms=None,backfilled=True,
                usable_for_historical_asof_training=False,cash_receipt=False)

def collect_one(slug):
    row=dict(slug=slug,label=None,error=None)
    try:
        g,gh,gt,gb=fetch_public(GAMMA+slug)
        row.update(gamma=g,gamma_raw_utf8=gb,gamma_sha256=gh,gamma_received_ms=gt,gamma_url=GAMMA+slug)
        cid=g.get('conditionId','')
        c={}
        if CID.fullmatch(cid):
            try:
                c,ch,ct,cb=fetch_public(CLOB+cid)
                row.update(clob=c,clob_raw_utf8=cb,clob_sha256=ch,clob_received_ms=ct,clob_url=CLOB+cid)
            except HTTPError as e:
                if e.code!=404: raise
                row['clob_unavailable']='HTTP404'
        row['label']=parse_evidence(slug,g,c,int(time.time()*1000))
    except AccessDenied: raise
    except Exception as e:row['error']=str(e)[:240]
    time.sleep(.2)
    return row

def run(output,hours=48,max_markets=576):
    if not 1<=hours<=48 or not 1<=max_markets<=576:raise ValueError('Bounded 48h/576 market scope')
    end=int(time.time())//300*300
    starts=list(range(end-hours*3600,end,300))[-max_markets:]
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    with (output/'public_outcomes.jsonl').open('w',encoding='utf8') as f:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            for r in ex.map(collect_one,[f'btc-updown-5m-{s}'for s in starts]):
                rows.append(r);f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
    content=(output/'public_outcomes.jsonl').read_bytes()
    manifest=dict(scope='bounded_public_outcome_evidence_only',markets=len(rows),
       labelled=sum(r['label'] is not None for r in rows),unknown=sum(r['label']is None for r in rows),
       sha256=hashlib.sha256(content).hexdigest(),start_epoch=starts[0],end_epoch=end,
       cash_verified=False,orders_enabled=False,created_ms=int(time.time()*1000))
    (output/'outcome_manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(manifest,indent=2))
    if not manifest['labelled']:raise RuntimeError('No explicit outcomes obtained; retained raw diagnostics')
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('outcome_evidence'));p.add_argument('--hours',type=int,default=48)
    a=p.parse_args();run(a.output,a.hours)
