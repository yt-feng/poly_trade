#!/usr/bin/env python3
"""Prepare one targeted cloud recovery for the still-incomplete homework PDF."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
SOURCE = STUDY / 'extract-0170-readback-20261026.json'
PREFLIGHT = STUDY / 'extract-0171-preflight-20261026.json'
DELIVERY = STUDY / 'extract-0171-preflight-delivery'
RECEIPT = STUDY / 'extract-0171-preflight-publication.json'
RECOVERY_SOURCE = '0175b33bb482ec5c37fb86b1c8d1015d7a37e09f6a36c6b6359b562f7cacd9d9'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
CELL_ID = 'quantml-extract-0171-preflight'
PUBLISHER_RUN_ID = '40302264902'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
    if any(p.exists() for p in (PREFLIGHT, DELIVERY, RECEIPT)):
        raise SystemExit('extract0171_preflight_already_exists')
    previous = json.loads(SOURCE.read_text(encoding='utf-8'))
    bounds = previous.get('source_receipts_bounds') or {}
    coverage = previous.get('coverage') or {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    preflight = {
        'format': 'quantml-extract-0171-preflight-v1',
        'created_utc': now,
        'batch_id': 'extract-0171-targeted-homework-recovery',
        'workflow': 'corpus-private-extract.yml',
        'mode': 'extract',
        'source_previous_batch': {
            'run_id': previous.get('workflow_run_id'),
            'readback': str(SOURCE),
            'metadata_cursor_committed': previous.get('metadata_cursor_committed'),
            'last_source_id': bounds.get('last_source_id'),
            'source_count_including_discovered_children': coverage.get('source_count_including_discovered_children'),
        },
        'targeted_recovery': {
            'source_ids': [RECOVERY_SOURCE],
            'reason': 'manifest marks this PDF page/visual coverage pending; source attribution review found only pages 1-2 returned',
            'cursor_commit_expected': False,
        },
        'cloud_execution': {
            'backend': 'GitHub Actions corpus-private-extract',
            'runner': 'ubuntu-latest',
            'expected_cpu': 'bounded worker batch only',
            'expected_memory': 'existing worker and 1 GiB output cap',
            'expected_runtime_minutes': 20,
            'concurrency': 1,
            'cache_identity': 'targeted source recovery after extract-0170 run 40302264902',
            'local_heavy_compute': False,
        },
        'parameters': {
            'pdf_ocr': True,
            'max_metadata': 4,
            'max_files': 4,
            'max_units': 40,
            'recovery_source_ids': [RECOVERY_SOURCE],
        },
        'gates': {
            'duplicate_dispatch': False,
            'prior_cursor_readback_verified': True,
            'semantic_credit_before': 0,
            'full_corpus_complete_before': False,
            'actual_fills_before': 0,
            'verified_canary_before': 0,
            'buying_paused': True,
        },
        'hypothesis': 'Recover remaining page/visual coverage for one explicitly attributed PDF; acquisition coverage only, not a BTC strategy experiment.',
        'success_contract': 'Persist encrypted report and direct readback; count only genuinely new page/visual evidence and preserve the 3492-source denominator.',
        'failure_contract': 'Retain terminal or unknown result and do not retry an uncertain mutation without readback.',
    }
    PREFLIGHT.write_text(json.dumps(preflight, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    PREFLIGHT.chmod(0o600)
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    DELIVERY.mkdir(mode=0o700)
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({
        'format': 'quantml-extract-0171-preflight-sealed-v1',
        'preflight': preflight,
        'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'actual_fills': 0, 'verified_canary_fills': 0,
        'live_orders_enabled': False, 'plaintext_published': False,
    }), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({
        'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
        'strategy_evaluated': False, 'live_orders_enabled': False,
        'plaintext_published': False, 'sealed_parts': 1,
    }) + b'\n', 0o644)
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools/publish_quality_first_journal.py'),
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
        '--run-id', PUBLISHER_RUN_ID, '--run-attempt', '1',
        '--recipient-id', RECIPIENT_ID, '--cell-id', CELL_ID,
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stdout.strip() or 'preflight_publication_failed')
    publication = json.loads(proc.stdout)
    write_json(RECEIPT, {
        'format': 'quantml-extract-0171-preflight-publication-v1',
        'cell_id': CELL_ID,
        'source': str(PREFLIGHT),
        'source_sha256': hashlib.sha256(PREFLIGHT.read_bytes()).hexdigest(),
        'publisher_run_id': PUBLISHER_RUN_ID,
        'publisher_run_attempt': 1,
        'recipient_id': RECIPIENT_ID,
        'publication': publication,
        'plaintext_published': False,
    })
    print(json.dumps({'status': publication.get('status'), 'target_source_id': RECOVERY_SOURCE}, ensure_ascii=False))


if __name__ == '__main__':
    main()
