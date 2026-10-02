"""Read-only research bundle: public archives, official labels, old research.
Never imports a trader or reads exchange/wallet credentials. No schedule or orders.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import urllib.error
import urllib.request

OUT = Path('research_bundle')
API = 'https://api.github.com/repos/yt-feng/poly'
MAX_BYTES = 300 * 1024 * 1024
used = 0
manifest = {'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'source_repository': 'yt-feng/poly', 'files': [], 'errors': [], 'limits':
            {'max_bytes': MAX_BYTES, 'release_pages': 3, 'max_capture_releases': 12,
             'raw_segments_latest_release': 5}}


def request(url: str) -> bytes:
    headers = {'User-Agent': 'poly-trade-readonly-research', 'Accept': 'application/json'}
    token = os.getenv('GH_TOKEN', '')
    if url.startswith('https://api.github.com/') and token:
        headers['Authorization'] = 'Bearer ' + token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 418, 429, 451):
                raise  # no alternate-host/region workarounds
            if e.code < 500 or attempt == 2:
                raise
        except (TimeoutError, OSError):
            if attempt == 2:
                raise
        time.sleep(2**attempt)
    raise RuntimeError('Request retries exhausted')


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> None:
    global used
    OUT.mkdir(exist_ok=True)
    inventory = []
    for folder in ('analysis', 'docs', 'reports', 'config'):
        for p in sorted(Path(folder).rglob('*')):
            if not p.is_file() or p.suffix not in ('.py', '.md', '.json', '.csv', '.yml', '.yaml'):
                continue
            if 'live_paper_trading' in p.parts or p.name.startswith('.'):
                continue
            inventory.append({'path': str(p), 'size': p.stat().st_size})
            if p.stat().st_size <= 4*1024*1024:
                dest = OUT/'old_research'/p
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
    for name in ('README.md', 'research_v19_historical_validation.py'):
        if Path(name).exists():
            shutil.copyfile(name, OUT/'old_research'/name)
    save_json(OUT/'old_research_inventory.json', inventory)
    releases = []
    for page in range(1, 4):
        response = json.loads(request(API + f'/releases?per_page=100&page={page}'))
        releases += [r for r in response if re.fullmatch(r'capture-v2-\d+-\d+', r['tag_name'])]
        if len(releases) >= 12 or len(response) < 100:
            break
    releases.sort(key=lambda r: r['published_at'], reverse=True)
    releases = releases[:12]
    save_json(OUT/'release_inventory.json', releases)
    for index, release in enumerate(releases):
        tag = release['tag_name']
        assets = release.get('assets', [])
        by_name = {a['name']: a for a in assets}
        raw = sorted((a for a in assets if a['name'].startswith('raw-') and a['name'].endswith('.jsonl.gz')), key=lambda a: a['name'])[-5:]
        wanted = [a for a in assets if a['name'].startswith(('snapshots-', 'labels-', 'markouts-')) and a['name'].endswith('.jsonl.gz')]
        wanted += [a for a in assets if a['name'] in ('health.json', 'manifest.json', 'quality.json')]
        if index == 0:
            wanted += raw
        for a in wanted:
            name = a['name']
            if Path(name).name != name:
                raise ValueError('Unsafe archive name')
            if used+a['size'] > MAX_BYTES:
                manifest['errors'].append({'tag': tag, 'name': name, 'error': 'explicit download budget limit'})
                continue
            try:
                content = request(a['browser_download_url'])
                digest = hashlib.sha256(content).hexdigest()
                if name.endswith('.gz'):
                    sidecar = by_name.get(name+'.sha256')
                    if not sidecar:
                        raise ValueError('Missing source checksum sidecar')
                    check = request(sidecar['browser_download_url'])
                    expected = check.decode().split()[0]
                    if expected != digest:
                        raise ValueError('Source checksum mismatch')
                dest = OUT/'captured'/tag/name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
                if name.endswith('.gz'):
                    dest.with_name(name+'.sha256').write_bytes(check)
                used += len(content)
                manifest['files'].append({'path': str(dest.relative_to(OUT)), 'url': a['browser_download_url'],
                                          'bytes': len(content), 'sha256': digest, 'release': tag})
            except Exception as e:
                manifest['errors'].append({'tag': tag, 'name': name, 'error': str(e)[:350]})
            save_json(OUT/'acquisition_manifest.json', manifest)
    # Retrieve only closed-event official metadata for outcome validation, never features.
    import gzip
    slugs = set()
    for p in (OUT/'captured').rglob('snapshots-*.jsonl.gz'):
        with gzip.open(p, 'rt') as f:
            for line in f:
                row = json.loads(line)
                if row.get('asset') == 'btc' and re.fullmatch(r'btc-updown-5m-\d+', row.get('slug', '')):
                    slugs.add(row['slug'])
    labels = []
    for slug in sorted(slugs)[-300:]:
        if int(slug.rsplit('-',1)[1])+300 >= time.time():
            continue
        url = 'https://gamma-api.polymarket.com/markets?slug='+slug
        try:
            payload = json.loads(request(url))
            labels.append({'slug': slug, 'retrieved_ms': int(time.time()*1000), 'url': url, 'payload': payload})
        except Exception as e:
            manifest['errors'].append({'slug': slug, 'error': str(e)[:200]})
        time.sleep(.12)
    save_json(OUT/'official_posthoc_markets.json', labels)
    manifest['snapshot_market_count'] = len(slugs)
    manifest['official_posthoc_queries'] = len(labels)
    manifest['downloaded_bytes'] = used
    save_json(OUT/'acquisition_manifest.json', manifest)
    print(json.dumps({'captured_files': len(manifest['files']), 'bytes': used, 'markets': len(slugs),
                      'errors': manifest['errors'][:20]}, indent=2))
    if not any('/snapshots-' in x['path'] for x in manifest['files']):
        raise RuntimeError('No checksum-verified production snapshots acquired')


if __name__ == '__main__':
    main()
