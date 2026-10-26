#!/usr/bin/env python3
"""Seal and idempotently publish the extract-0168 checkpoint reconciliation.

Only the compact control-plane audit is sealed.  No corpus, credentials, or
plaintext findings are published, and the helper never starts a research run.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
SOURCE = BASE / 'extract-0168-checkpoint-reconciliation-20261026.json'
OUT = BASE / 'extract-0168-checkpoint-reconciliation-delivery'
RECEIPT = BASE / 'extract-0168-checkpoint-reconciliation-publication.json'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
CELL_ID = 'quantml-extract-0168-checkpoint-reconciliation'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
    if RECEIPT.is_file():
        existing = json.loads(RECEIPT.read_text(encoding='utf-8'))
        if existing.get('publication', {}).get('status') == 'verified_remote':
            print(json.dumps({'status': 'already_verified', 'cell_id': CELL_ID}, ensure_ascii=False))
            return
    if not SOURCE.is_file():
        raise SystemExit('missing_reconciliation_audit')
    audit = json.loads(SOURCE.read_text(encoding='utf-8'))
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    payload = {
        'format': 'quantml-extract-0168-checkpoint-reconciliation-sealed-v1',
        'audit': audit,
        'plaintext_published': False,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    OUT.mkdir(mode=0o700, exist_ok=True)
    part = OUT / 'part-0000.vault'
    status = OUT / 'status.json'
    if not part.exists():
        vault.write_new(part, vault.seal(vault.canonical(payload), recipient, RECIPIENT_ID), 0o600)
    if not status.exists():
        vault.write_new(status, vault.canonical({
            'version': 1,
            'status': 'complete',
            'stage': 'sealed_delivery',
            'strategy_evaluated': False,
            'live_orders_enabled': False,
            'plaintext_published': False,
            'sealed_parts': 1,
        }) + b'\n', 0o644)
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools/publish_quality_first_journal.py'),
        '--source', str(OUT), '--repository', 'yt-feng/poly_trade', '--run-id', '40288936144',
        '--run-attempt', '1', '--recipient-id', RECIPIENT_ID, '--cell-id', CELL_ID,
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stdout.strip() or 'publication_failed')
    publication = json.loads(proc.stdout)
    result = {
        'schema': 'quantml-encrypted-journal-publication-v1',
        'cell_id': CELL_ID,
        'source': str(SOURCE),
        'recipient_id': RECIPIENT_ID,
        'plaintext_published': False,
        'publication': publication,
    }
    write_json(RECEIPT, result)
    print(json.dumps({'status': publication.get('status'), 'cell_id': CELL_ID}, ensure_ascii=False))


if __name__ == '__main__':
    main()
