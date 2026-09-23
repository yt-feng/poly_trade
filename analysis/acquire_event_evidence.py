"""Download bounded PUBLIC raw-message samples; never evaluate or trade a strategy.
Uses the existing snapshot acquisition manifest to select its newest release.
The sample is not a complete tape and cannot certify real order/fill latency.
"""
from __future__ import annotations
import argparse, gzip, hashlib, io, json, os, re, time, urllib.request
from collections import Counter
from pathlib import Path
API='https://api.github.com/repos/yt-feng/poly/'
DOWNLOAD='https://github.com/yt-feng/poly/releases/download/'
MAX_BYTES=32*1024*1024

def read_public(url,limit):
    if not (url.startswith(API) or url.startswith(DOWNLOAD)):
        raise ValueError('Unexpected source URL')
    h={'User-Agent':'poly-public-event-evidence','Accept':'application/vnd.github+json'}
    if url.startswith(API) and os.environ.get('GH_TOKEN'):
        h['Authorization']='Bearer '+os.environ['GH_TOKEN']
    # HTTP auth/rate/region failures are not bypassed or retried at other hosts.
    with urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=60)as r:
        declared=r.headers.get('Content-Length')
        if declared and int(declared)>limit:raise ValueError('Declared response exceeds limit')
        data=r.read(limit+1)
        if len(data)>limit:raise ValueError('Response exceeds limit')
        return data

def safe_name(name):
    return bool(re.fullmatch(r'raw-\d{4}-\d{2}-\d{2}-\d{6}\.jsonl\.gz',name))

def select_assets(assets,budget=MAX_BYTES,maximum=2):
    chosen=[];skipped=[];used=0
    for a in sorted((a for a in assets if safe_name(str(a.get('name','')))),key=lambda a:a['name'],reverse=True):
        size=int(a.get('size',0))
        if len(chosen)>=maximum:break
        if size<=0 or used+size>budget:skipped.append({'name':a['name'],'reason':'sample byte budget'});continue
        chosen.append(a);used+=size
    return chosen,skipped

def verify_and_describe(body,expected):
    actual=hashlib.sha256(body).hexdigest()
    if actual!=expected:raise ValueError('Source SHA256 mismatch')
    counts=Counter();lo=hi=None;decoded=0;rows=0
    with gzip.GzipFile(fileobj=io.BytesIO(body))as f:
        while True:
            line=f.readline(16*1024*1024+1)
            if not line:break
            decoded+=len(line)
            if len(line)>16*1024*1024 or decoded>160*1024*1024:raise ValueError('Decompression budget exceeded')
            x=json.loads(line);rows+=1;counts[str(x.get('source','unknown'))]+=1
            ns=x.get('received_at_ns')
            if isinstance(ns,int):lo=ns if lo is None else min(lo,ns);hi=ns if hi is None else max(hi,ns)
    return {'sha256':actual,'bytes':len(body),'rows':rows,'source_counts':dict(counts),'first_received_ns':lo,'last_received_ns':hi}

def acquire(manifest_path,output):
    m=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if m.get('source_repository')!='yt-feng/poly':raise ValueError('Source identity mismatch')
    releases=m.get('releases',[])
    if not releases:raise ValueError('No input releases')
    release=max(releases,key=lambda r:str(r.get('published_at','')))
    rid=int(release['id']);tag=str(release.get('tag',''))
    if not re.fullmatch(r'capture-v2-\d+-\d+',tag):raise ValueError('Unexpected release tag')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    result={'source_repository':'yt-feng/poly','release':tag,'created_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
      'max_selected_segments':2,'max_bytes':MAX_BYTES,'files':[],'errors':[],'full_tape_complete':False,
      'strategy_evaluated':False,'live_trading_enabled':False,'note':'Selected latest closed raw segments only; message counts are not independent trades or proof of capture completeness.'}
    try:
        assets=[]
        for page in range(1,31):
            batch=json.loads(read_public(API+f'releases/{rid}/assets?per_page=100&page={page}',4*1024*1024))
            if not isinstance(batch,list):raise ValueError('Unexpected assets response')
            assets.extend(batch)
            if len(batch)<100:break
        else:raise ValueError('Asset pagination limit')
        selected,result['skipped']=select_assets(assets);by_name={a['name']:a for a in assets};used=0
        if not selected:raise ValueError('No raw segment within sample budget')
        for a in selected:
            name=a['name'];side=by_name.get(name+'.sha256')
            if not side:raise ValueError('Missing source checksum')
            expected_prefix=DOWNLOAD+tag+'/'
            if not str(a['browser_download_url']).startswith(expected_prefix)or not str(side['browser_download_url']).startswith(expected_prefix):raise ValueError('Release URL mismatch')
            check=read_public(side['browser_download_url'],4096)
            digest=check.decode('utf-8').strip().split()[0].lower()
            body=read_public(a['browser_download_url'],MAX_BYTES-used);used+=len(body)
            info=verify_and_describe(body,digest)
            (output/name).write_bytes(body);(output/(name+'.sha256')).write_bytes(check)
            result['files'].append(dict(info,name=name,asset_id=a['id'],url=a['browser_download_url']))
    except Exception as e:
        result['errors'].append(str(e)[:300]);raise
    finally:
        (output/'event_evidence_manifest.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2))
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();acquire(a.manifest,a.output)
