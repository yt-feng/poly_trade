#!/usr/bin/env python3
"""Seal the reproducible QuantML delivery-order root-cause repair."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
SOURCE = STUDY / 'quantml-delivery-priority-repair-20261026.json'
DELIVERY = STUDY / 'quantml-delivery-priority-repair-delivery'
RECEIPT = STUDY / 'quantml-delivery-priority-repair-publication.json'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
RUN_ID = '40304795524'
CELL_ID = 'quantml-delivery-priority-repair'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    temp = path.with_name('.' + path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def main() -> None:
    if RECEIPT.is_file():
        current = json.loads(RECEIPT.read_text(encoding='utf-8'))
        if current.get('publication', {}).get('status') == 'verified_remote':
            print(json.dumps({'status': 'already_verified', 'cell_id': CELL_ID}, ensure_ascii=False))
            return
    summary = {
        'format': 'quantml-delivery-priority-repair-v1',
        'run_id_exposing_issue': 40304795524,
        'observed_issue': 'current cursor tranche selected four new metadata sources but returned 49 stale transport rows from two older sources',
        'root_cause': 'build_unit_delivery iterated the durable unit-state export in source order; old unacknowledged evidence consumed the bounded report budget before current selected-manifest units',
        'repair': 'prioritize selected_manifest source ids and discovered parent-child descendants, then fall back to the existing queue order',
        'repair_commit': '2c354c83',
        'tests': {'test_corpus_extract': 50, 'test_corpus_worker_ocr': 7, 'test_corpus_worker_recovery': 8},
        'cloud_validation_pending': 'one future normal cursor batch on the repair commit; no duplicate 0172 dispatch',
        'semantic_credit': 0,
        'full_corpus_complete': False,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
        'next_action': 'Dispatch one distinct cloud cursor tranche on the repair commit only after the current checkpoint remains idle; verify current-source attribution before any semantic claim.',
    }
    write_json(SOURCE, summary)
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    DELIVERY.mkdir(mode=0o700, exist_ok=True)
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({
        'format': 'quantml-delivery-priority-repair-sealed-v1',
        'summary': summary,
        'plaintext_published': False,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({
        'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
        'strategy_evaluated': False, 'live_orders_enabled': False,
        'plaintext_published': False, 'sealed_parts': 1,
    }) + b'\n', 0o644)
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools/publish_quality_first_journal.py'),
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
        '--run-id', RUN_ID, '--run-attempt', '1', '--recipient-id', RECIPIENT_ID,
        '--cell-id', CELL_ID,
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stdout.strip() or 'publication_failed')
    publication = json.loads(proc.stdout)
    write_json(RECEIPT, {
        'format': 'quantml-delivery-priority-repair-publication-v1',
        'cell_id': CELL_ID,
        'source': str(SOURCE),
        'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'recipient_id': RECIPIENT_ID,
        'plaintext_published': False,
        'publication': publication,
    })
    print(json.dumps({'status': publication.get('status'), 'cell_id': CELL_ID}, ensure_ascii=False))


if __name__ == '__main__':
    main()
