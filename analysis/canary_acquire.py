"""Download public, checksummed research inputs. No exchange accounts or orders."""
from __future__ import annotations
import argparse, gzip, hashlib, json, os, re, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API = 'https://api.github.com/repos/yt-feng/poly'

def request(url: str, limit: int = 100*1024*1024) -> bytes:
    headers = {'User-Agent': 'poly-canary-research', 'Accept': 'application/json'}
    if url.startswith('https://api.github.com/') and os.getenv('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=90) as r:
                data = r.read(limit+1)
                if len(data)>limit: raise ValueError('Response exceeds download budget')
                return data
        except HTTPError as e:
            if e.code < 500 or attempt == 2: raise
        except (TimeoutError, OSError):
            if attempt == 2: raise
        time.sleep(2**attempt)
    raise RuntimeError('Retries exhausted')

def main():
    p=argparse.ArgumentParser();p.add_argument('--output', type=Path, default=Path('canary_inputs'))
    p.add_argument('--max-bytes',type=int,default=220*1024*1024); a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    records=[]; errors=[]; used=0
    releases=json.loads(request(API+'/releases?per_page=40'))
    releases=[r for r in releases if re.fullmatch(r'capture-v2-\d+-\d+',r['tag_name'])][:4]
    (a.output/'releases.json').write_text(json.dumps(releases,indent=2))
    for index, r in enumerate(releases):
        assets={x['name']:x for x in r['assets']}
        names=[n for n in assets if (n.startswith(('snapshots-','labels-')) and n.endswith('.jsonl.gz')) or n in ('health.json','manifest.json')]
        # Original frames only for the newest production release, oldest first.
        if index==0:
            names+=sorted(n for n in assets if n.startswith('raw-') and n.endswith('.jsonl.gz'))[:24]
        for n in names:
            if Path(n).name != n: raise ValueError('Unsafe filename')
            item=assets[n]
            if used+item['size']>a.max_bytes:
                errors.append({'release':r['tag_name'],'name':n,'error':'budget'});continue
            try:
                data=request(item['browser_download_url'],a.max_bytes-used)
                digest=hashlib.sha256(data).hexdigest()
                check=None
                if n.endswith('.gz'):
                    check=request(assets[n+'.sha256']['browser_download_url'],4096)
                    if digest!=check.decode().split()[0]: raise ValueError('SHA256 mismatch')
                dest=a.output/'captured'/r['tag_name']/n;dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(data)
                if check:dest.with_name(n+'.sha256').write_bytes(check)
                used+=len(data)
                records.append({'path':str(dest.relative_to(a.output)),'sha256':digest,'bytes':len(data),'url':item['browser_download_url']})
            except Exception as e:errors.append({'release':r['tag_name'],'name':n,'error':str(e)[:200]})
    # Preserve current rule parsing and archive code for exact reproducibility.
    for n in ('capture_v3.py','microstructure_v3.py','microstructure_math_v3.py'):
        try:
            data=request('https://raw.githubusercontent.com/yt-feng/poly/main/'+n)
            d=a.output/'collector_source'/n;d.parent.mkdir(parents=True,exist_ok=True);d.write_bytes(data)
        except Exception as e:errors.append({'name':n,'error':str(e)[:200]})
    summary={'retrieved_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'files':records,'errors':errors,'downloaded_bytes':used,'max_bytes':a.max_bytes,'read_only':True}
    (a.output/'manifest.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps({'files':len(records),'bytes':used,'errors':errors},indent=2))
    if not any('/snapshots-' in f['path'] for f in records):raise RuntimeError('No verified snapshots')

if __name__=='__main__':main()
