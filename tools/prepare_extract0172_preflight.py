#!/usr/bin/env python3
"""Prepare and seal one distinct normal QuantML cursor continuation."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
N = os.environ.get('EXTRACT_BATCH', '0172')
PREVIOUS_RUN = int(os.environ.get('EXTRACT_PRIOR_RUN', '40302264902'))
SOURCE = STUDY / f'extract-{os.environ.get("EXTRACT_PRIOR_BATCH", "0170")}-readback-20261026.json'
PREFLIGHT = STUDY / f'extract-{N}-preflight-20261026.json'
DELIVERY = STUDY / f'extract-{N}-preflight-delivery'
RECEIPT = STUDY / f'extract-{N}-preflight-publication.json'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
CELL_ID = 'quantml-extract-0172-preflight'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
    if any(path.exists() for path in (PREFLIGHT, DELIVERY, RECEIPT)):
        raise SystemExit('extract0172_preflight_already_exists')
    previous = json.loads(SOURCE.read_text(encoding='utf-8'))
    if previous.get('workflow_run_id') != PREVIOUS_RUN or previous.get('metadata_cursor_committed') is not True:
        raise SystemExit('authoritative_cursor_readback_mismatch')
    bounds = previous.get('source_receipts_bounds') or {}
    coverage = previous.get('coverage') or {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    preflight = {
        'format': f'quantml-extract-{N}-preflight-v1',
        'created_utc': now,
        'batch_id': f'extract-{N}-normal-distinct-cursor-continuation',
        'workflow': 'corpus-private-extract.yml',
        'mode': 'extract',
        'source_previous_batch': {
            'run_id': PREVIOUS_RUN,
            'readback': str(SOURCE),
            'metadata_cursor_committed': True,
            'last_source_id': bounds.get('last_source_id'),
            'source_count_including_discovered_children': coverage.get('source_count_including_discovered_children'),
        },
        'targeted_recovery': {
            'source_ids': [],
            'cursor_commit_expected': True,
            'reason': f'normal distinct cursor tranche after {os.environ.get("EXTRACT_PRIOR_BATCH", "0170")}; prior targeted recovery was readback-only and is not replayed',
        },
        'cloud_execution': {
            'backend': 'GitHub Actions corpus-private-extract',
            'runner': 'ubuntu-latest',
            'expected_cpu': 'bounded worker batch only',
            'expected_memory': 'existing worker and 1 GiB output cap',
            'expected_runtime_minutes': 20,
            'concurrency': 1,
            'cache_identity': 'normal distinct cursor after committed extract-0170',
            'local_heavy_compute': False,
        },
        'parameters': {
            'pdf_ocr': True,
            'max_metadata': 4,
            'max_files': 4,
            'max_units': 40,
            'recovery_source_ids': [],
            'no_strategy_evaluation': True,
            'no_model_refit': True,
            'no_live_orders': True,
        },
        'gates': {
            'duplicate_dispatch': False,
            'prior_terminal_readback_verified': True,
            'targeted_0171_replayed': False,
            'ack_input_not_read': True,
            'semantic_credit_before': 0,
            'full_corpus_complete_before': False,
            'actual_fills_before': 0,
            'verified_canary_before': 0,
            'buying_paused': True,
        },
        'hypothesis': f'A distinct normal cursor tranche {N} increases QuantML coverage without replaying targeted recovery or treating transport as semantic learning.',
        'success_contract': 'Persist encrypted report and direct readback; count only newly committed units while preserving the 3492-source denominator.',
        'failure_contract': 'Retain terminal or unknown result and do not retry an uncertain dispatch without readback; keep semantic credit 0 and trading disabled.',
    }
    write_json(PREFLIGHT, preflight)
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    DELIVERY.mkdir(mode=0o700)
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({
        'format': f'quantml-extract-{N}-preflight-sealed-v1',
        'preflight': preflight,
        'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
    }), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({
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
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
        '--run-id', str(PREVIOUS_RUN), '--run-attempt', '1',
        '--recipient-id', RECIPIENT_ID, '--cell-id', CELL_ID,
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stdout.strip() or 'preflight_publication_failed')
    publication = json.loads(proc.stdout)
    write_json(RECEIPT, {
        'format': f'quantml-extract-{N}-preflight-publication-v1',
        'cell_id': CELL_ID,
        'source': str(PREFLIGHT),
        'source_sha256': hashlib.sha256(PREFLIGHT.read_bytes()).hexdigest(),
        'publisher_run_id': str(PREVIOUS_RUN),
        'publisher_run_attempt': 1,
        'recipient_id': RECIPIENT_ID,
        'publication': publication,
        'plaintext_published': False,
    })
    print(json.dumps({'status': publication.get('status'), 'batch': N, 'prior_cursor_run': PREVIOUS_RUN}, ensure_ascii=False))


if __name__ == '__main__':
    main()
