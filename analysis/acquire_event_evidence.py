"""Download bounded PUBLIC raw-message samples; never evaluate or trade a strategy.
Uses the existing snapshot acquisition manifest to select its newest release.
The sample is not a complete tape and cannot certify real order/fill latency.
"""
from __future__ import annotations
import argparse, gzip, hashlib, http.client, io, json, os, re, time, urllib.error, urllib.request
from collections import Counter
from pathlib import Path
from forward_acquire import AccessRefused, REFUSALS, ResponseError, TransportError, _retry_wait
API='https://api.github.com/repos/yt-feng/poly/'
DOWNLOAD='https://github.com/yt-feng/poly/releases/download/'
MAX_BYTES=32*1024*1024

def read_public(url,limit,*,json_response=False):
    if not (url.startswith(API) or url.startswith(DOWNLOAD)):
        raise ValueError('Unexpected source URL')
    h={'User-Agent':'poly-public-event-evidence','Accept':'application/vnd.github+json'}
    if url.startswith(API) and os.environ.get('GH_TOKEN'):
        h['Authorization']='Bearer '+os.environ['GH_TOKEN']
    # Retry the same request, never reselect a sample or bypass access refusals.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=60)as r:
                declared=r.headers.get('Content-Length')
                if declared and int(declared)>limit:raise ValueError('Declared response exceeds limit')
                data=r.read(limit+1)
                if len(data)>limit:raise ValueError('Response exceeds limit')
                if declared is not None and len(data)!=int(declared):
                    raise ResponseError('RESPONSE_LENGTH_MISMATCH')
                if json_response:
                    try:
                        return json.loads(data)
                    except (json.JSONDecodeError,UnicodeDecodeError):
                        raise ResponseError('INVALID_JSON_RESPONSE') from None
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in REFUSALS:
                raise AccessRefused(exc.code,False,attempt+1) from None
            retryable=500<=exc.code<=599
            if not retryable or attempt==3:
                raise TransportError(exc.code,retryable,attempt+1) from None
            delay=_retry_wait(exc.headers,attempt)
        except (ResponseError,http.client.IncompleteRead,http.client.RemoteDisconnected):
            if attempt==3:
                raise ResponseError('INCOMPLETE_HTTP_RESPONSE',attempt+1) from None
            delay=_retry_wait(None,attempt)
        except (TimeoutError,OSError):
            if attempt==3:
                raise TransportError(None,True,attempt+1) from None
            delay=_retry_wait(None,attempt)
        time.sleep(delay)
    raise AssertionError('unreachable')

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

def acquire(manifest_path,output,status=None):
    status={} if status is None else status
    status['phase']='input_validation'
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
        status['phase']='asset_listing'
        assets=[]
        for page in range(1,31):
            batch=read_public(API+f'releases/{rid}/assets?per_page=100&page={page}',4*1024*1024,json_response=True)
            if not isinstance(batch,list):raise ValueError('Unexpected assets response')
            assets.extend(batch)
            if len(batch)<100:break
        else:raise ValueError('Asset pagination limit')
        status['phase']='sample_selection'
        selected,result['skipped']=select_assets(assets);by_name={a['name']:a for a in assets};used=0
        if not selected:raise ValueError('No raw segment within sample budget')
        for a in selected:
            name=a['name'];side=by_name.get(name+'.sha256')
            if not side:raise ValueError('Missing source checksum')
            expected_prefix=DOWNLOAD+tag+'/'
            if not str(a['browser_download_url']).startswith(expected_prefix)or not str(side['browser_download_url']).startswith(expected_prefix):raise ValueError('Release URL mismatch')
            status['phase']='checksum_download'
            check=read_public(side['browser_download_url'],4096)
            digest=check.decode('utf-8').strip().split()[0].lower()
            status['phase']='segment_download'
            body=read_public(a['browser_download_url'],MAX_BYTES-used);used+=len(body)
            status['phase']='segment_verification'
            info=verify_and_describe(body,digest)
            status['phase']='evidence_save'
            (output/name).write_bytes(body);(output/(name+'.sha256')).write_bytes(check)
            result['files'].append(dict(info,name=name,asset_id=a['id'],url=a['browser_download_url']))
    except Exception as e:
        result['errors'].append(str(e)[:300]);raise
    finally:
        (output/'event_evidence_manifest.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2))
    return result

def public_status(status,error=None):
    """Only fixed categories and typed transport fields may leave private stdout."""
    phases={'input_validation','asset_listing','sample_selection','checksum_download',
            'segment_download','segment_verification','evidence_save'}
    phase=status.get('phase')
    transport=isinstance(error,TransportError)
    code=getattr(error,'code',None) if transport else None
    attempts=getattr(error,'attempts',None) if transport else None
    category=('access_refused' if isinstance(error,AccessRefused) else
              'incomplete_response' if isinstance(error,ResponseError) else
              'transport' if transport else
              'validation' if isinstance(error,(ValueError,EOFError,gzip.BadGzipFile)) else
              'unexpected_failure' if error is not None else None)
    return {'status_contract_version':1,'scope':'bounded_public_sample_integrity_only',
            'completed':error is None,'phase':phase if phase in phases else 'unknown',
            'failure_category':category,
            'http_status':code if type(code) is int and 100<=code<=599 else None,
            'attempts':attempts if type(attempts) is int and 1<=attempts<=4 else None,
            'retryable':error.retryable if transport and type(error.retryable) is bool else False,
            'access_refused':isinstance(error,AccessRefused),
            'full_tape_complete':False,'strategy_evaluated':False,'live_trading_enabled':False}

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--status-file',type=Path)
    args=p.parse_args(argv)
    status={};error=None
    try:
        acquire(args.manifest,args.output,status=status)
    except Exception as exc:
        error=exc
        raise
    finally:
        if args.status_file is not None:
            args.status_file.parent.mkdir(parents=True,exist_ok=True)
            args.status_file.write_text(json.dumps(public_status(status,error),indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
