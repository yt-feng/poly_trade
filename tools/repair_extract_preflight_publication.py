#!/usr/bin/env python3
"""Resume a preflight publication after a stable-cell episode conflict."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
N = os.environ.get('EXTRACT_BATCH', '0183')
RUN_ID = int(os.environ['EXTRACT_PRIOR_RUN'])
CELL_ID = os.environ.get('EXTRACT_PREFLIGHT_CELL', f'quantml-extract-{N}-preflight')
PREFLIGHT = STUDY / f'extract-{N}-preflight-20261026.json'
DELIVERY = STUDY / f'extract-{N}-preflight-delivery'
RECEIPT = STUDY / f'extract-{N}-preflight-publication.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
    if not PREFLIGHT.is_file() or not DELIVERY.is_dir():
        raise SystemExit('preflight_delivery_missing')
    if RECEIPT.exists():
        existing = json.loads(RECEIPT.read_text(encoding='utf-8'))
        if (existing.get('publication') or {}).get('status') == 'verified_remote':
            print(json.dumps({'status': 'already_verified', 'batch': N}, ensure_ascii=False))
            return
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools/publish_quality_first_journal.py'),
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
        '--run-id', str(RUN_ID), '--run-attempt', '1', '--recipient-id', RECIPIENT_ID,
        '--cell-id', CELL_ID,
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stdout.strip() or 'preflight_publication_failed')
    publication = json.loads(proc.stdout.strip().splitlines()[-1])
    write_json(RECEIPT, {
        'format': f'quantml-extract-{N}-preflight-publication-v1',
        'cell_id': CELL_ID,
        'source': str(PREFLIGHT),
        'source_sha256': hashlib.sha256(PREFLIGHT.read_bytes()).hexdigest(),
        'publisher_run_id': str(RUN_ID),
        'publisher_run_attempt': 1,
        'recipient_id': RECIPIENT_ID,
        'publication': publication,
        'plaintext_published': False,
    })
    print(json.dumps({'status': publication.get('status'), 'batch': N, 'cell_id': CELL_ID}, ensure_ascii=False))


if __name__ == '__main__':
    main()
