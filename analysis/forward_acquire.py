"""Bounded public-data acquisition. No exchange credentials or order endpoints."""
from __future__ import annotations
import argparse, hashlib, json, os, re, time, urllib.request, urllib.error
from pathlib import Path

API='https://api.github.com/repos/yt-feng/poly'
PREFIX='https://github.com/yt-feng/poly/releases/download/'
LIMIT=256*1024*1024

def get(url, limit=32*1024*1024):
    if not (url.startswith(API+'/') or url.startswith(PREFIX)):
        raise ValueError('Unexpected source host/path')
    headers={'User-Agent':'poly-trade-forward-readonly','Accept':'application/json'}
    if url.startswith(API+'/') and os.getenv('GH_TOKEN'):
        headers['Authorization']='Bearer '+os.environ['GH_TOKEN']
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=90) as r:
                data=r.read(limit+1)
                if len(data)>limit:raise ValueError('Response exceeds size budget')
                return data
        except urllib.error.HTTPError as e:
            if e.code<500 or attempt==2:raise
        except (TimeoutError,OSError):
            if attempt==2:raise
        time.sleep(2**attempt)
    raise RuntimeError('Download failed')

def acquire(output, since='2026-09-23', seen=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    seen=seen or {}
    manifest={'source_repository':'yt-feng/poly','created_ms':int(time.time()*1000),'files':[],
              'errors':[],'cached_files':0,'downloaded_bytes':0,'inventory_complete':False}
    releases=[]
    for page in range(1,11):
        group=json.loads(get(API+f'/releases?per_page=100&page={page}'))
        releases.extend(r for r in group if re.fullmatch(r'capture-v2-\d+-\d+',r.get('tag_name',''))
                        and r.get('published_at','')[:10]>=since)
        if len(group)<100:
            manifest['inventory_complete']=True;break
    if not manifest['inventory_complete']:manifest['errors'].append('release pagination cap reached')
    for release in sorted(releases,key=lambda r:r['published_at']):
        tag=release['tag_name'];assets={}
        for page in range(1,31):
            group=json.loads(get(API+f'/releases/{release["id"]}/assets?per_page=100&page={page}'))
            assets.update({a['name']:a for a in group})
            if len(group)<100:break
        else:manifest['errors'].append('asset pagination cap: '+tag)
        for name,a in sorted(assets.items()):
            if not (name.startswith(('snapshots-','labels-')) and name.endswith('.jsonl.gz')):continue
            if Path(name).name!=name:raise ValueError('Unsafe asset filename')
            key=tag+'/'+name
            # Same source asset ID is an immutable-input assumption; changes require fresh validation.
            if seen.get(key,{}).get('asset_id')==a['id']:
                manifest['cached_files']+=1;continue
            if manifest['downloaded_bytes']+a['size']>LIMIT:
                manifest['errors'].append('download budget: '+key);continue
            try:
                check=assets.get(name+'.sha256')
                if not check:raise ValueError('Missing checksum')
                data=get(a['browser_download_url']);sha=hashlib.sha256(data).hexdigest()
                side=get(check['browser_download_url'],4096)
                if side.decode().split()[0].lower()!=sha:raise ValueError('Checksum mismatch')
                dest=output/tag/name;dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(data);dest.with_name(name+'.sha256').write_bytes(side)
                manifest['downloaded_bytes']+=len(data)
                manifest['files'].append({'path':key,'sha256':sha,'bytes':len(data),'asset_id':a['id'],
                                          'url':a['browser_download_url']})
            except Exception as e:manifest['errors'].append(key+': '+str(e)[:200])
    manifest['complete_this_inventory']=manifest['inventory_complete'] and not manifest['errors']
    (output/'acquisition.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in manifest.items() if k!='files'},indent=2))
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('forward_inputs'))
    p.add_argument('--since',default='2026-09-23');a=p.parse_args()
    m=acquire(a.output,a.since)
    if not m['files']:raise SystemExit('No new verified files; inspect acquisition.json')
