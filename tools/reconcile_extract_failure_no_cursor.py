#!/usr/bin/env python3
"""Reconcile a terminal extract failure/readback with no cursor advance.

This is deliberately aggregate-only: it preserves the last committed cursor,
records the worker failure and compact readback counts, and seals that decision
through the existing journal publisher.  It never credits units, semantic
learning, strategy evaluations, or fills from a pre-failure snapshot.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
BATCH = os.environ.get('EXTRACT_BATCH', '0187')
RUN_ID = int(os.environ['EXTRACT_RUN_ID'])
PRIOR_RUN = int(os.environ['EXTRACT_PRIOR_RUN'])
READBACK = STUDY / f'extract-{BATCH}-readback-20261026.json'
OUT = STUDY / f'extract-{BATCH}-failure-no-cursor-20261026.json'
DELIVERY = STUDY / f'extract-{BATCH}-failure-no-cursor-delivery'
RECEIPT = STUDY / f'extract-{BATCH}-failure-no-cursor-publication.json'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'not_object:{path}')
    return value


def write(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.failure.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> None:
    if OUT.exists() or DELIVERY.exists() or RECEIPT.exists():
        raise SystemExit('failure_reconciliation_already_exists')
    readback = load(READBACK)
    if readback.get('workflow_run_id') != RUN_ID:
        raise RuntimeError('run_identity_mismatch')
    if readback.get('metadata_cursor_committed') is True:
        raise RuntimeError('cursor_was_committed_use_success_reconciler')
    sealed = readback.get('sealed_journal') or {}
    if sealed.get('status') != 'verified_remote':
        raise RuntimeError('readback_not_sealed')
    coverage = readback.get('coverage') or {}
    proposal = readback.get('unit_delivery_proposal') or {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    action = (
        f'extract-{BATCH} is terminal with no committed cursor; preserve authoritative run {PRIOR_RUN}, '
        'do not count the repeated snapshot, and choose a future distinct tranche only after deduplication review. '
        'Semantic credit remains 0 and buying stays paused.'
    )
    summary = {
        'format': f'quantml-extract-{BATCH}-failure-no-cursor-v1',
        'created_utc': now,
        'workflow_run_id': RUN_ID,
        'previous_authoritative_cursor_run': PRIOR_RUN,
        'metadata_cursor_committed': False,
        'remote_failure_class': 'cloud_worker_timeout_or_non_successful_acquisition',
        'readback_verified_remote': True,
        'source_count_snapshot': coverage.get('source_count_including_discovered_children'),
        'units_snapshot_not_credited': coverage.get('units_written_this_run'),
        'bytes_snapshot_not_credited': coverage.get('bytes_written_this_run'),
        'transport_evidence_observed_not_credited': proposal.get('evidence_count'),
        'semantic_credit': 0,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'full_corpus_complete': False,
        'duplicate_dispatch': False,
        'cursor_advance_credited': False,
        'plaintext_published': False,
        'next_action': action,
    }
    write(OUT, summary)
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    DELIVERY.mkdir(mode=0o700)
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({
        'format': f'quantml-extract-{BATCH}-failure-no-cursor-sealed-v1',
        'summary': summary,
        'plaintext_published': False,
    }), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({
        'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
        'strategy_evaluated': False, 'live_orders_enabled': False,
        'plaintext_published': False, 'sealed_parts': 1,
    }) + b'\n', 0o644)
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools/publish_quality_first_journal.py'),
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
        '--run-id', str(RUN_ID), '--run-attempt', '1',
        '--recipient-id', RECIPIENT_ID,
        '--cell-id', f'quantml-extract-{BATCH}-failure-no-cursor',
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode:
        raise SystemExit(proc.stdout.strip() or 'failure_reconciliation_publication_failed')
    publication = json.loads(proc.stdout.strip().splitlines()[-1])
    write(RECEIPT, {
        'format': f'quantml-extract-{BATCH}-failure-no-cursor-publication-v1',
        'cell_id': f'quantml-extract-{BATCH}-failure-no-cursor',
        'source': str(OUT),
        'source_sha256': hashlib.sha256(OUT.read_bytes()).hexdigest(),
        'publisher_run_id': str(RUN_ID),
        'recipient_id': RECIPIENT_ID,
        'plaintext_published': False,
        'publication': publication,
    })
    paths = [BASE / 'active-episode.json', BASE / 'cycle-progress.json',
             BASE / 'quantml-learning-progress.json', STUDY / 'manifest-summary.json',
             STUDY / 'runtime-state.json', STUDY / 'progress.json']
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        state.update({
            'status': f'extract_{BATCH}_readback_verified_failure_no_cursor',
            'state': 'cloud_worker_failure_repeated_snapshot_no_cursor',
            'active_remote_task': None, 'active_run': None,
            'current_inflight_run': None, 'current_inflight_runs': [],
            'current_cloud_run_status': 'completed_failure_no_cursor',
            'quantml_inflight_extract_run': None,
            'quantml_inflight_extract_batch': None,
            'quantml_inflight_extract_status': f'completed_failure_no_cursor_{BATCH}',
            'latest_quantml_extract_run': RUN_ID,
            'latest_quantml_extract_status': 'completed_failure_no_cursor',
            'latest_quantml_extract_readback': str(READBACK),
            'latest_quantml_extract_failure': str(OUT),
            'authoritative_cursor_run': PRIOR_RUN,
            'authoritative_cursor_status': 'committed_before_failed_extract',
            'latest_quantml_extract_cursor_committed': False,
            'semantic_credit': 0, 'quantml_semantic_read_count': 0,
            'semantic_learning_complete': False, 'full_corpus_complete': False,
            'actual_fills': 0, 'verified_canary_fills': 0,
            'live_orders_enabled': False, 'buying_paused': True,
            'next_action': action, 'next_step': action,
            'quantml_next_action': action, 'checkpoint_updated_utc': now,
            'updated_utc': now, 'updated_at_utc': now,
        })
        write(path, state)
    for path in [BASE / 'QuantML-学习进度.md', STUDY / 'QuantML-学习进度.md']:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        marker = f'## EXTRACT-{BATCH} 云端失败回读（不推进游标）（2026-10-26）'
        if marker not in text:
            text += (
                f'\n\n{marker}\n\n'
                f'- run `{RUN_ID}` 在 acquisition 阶段超时/失败；direct readback 已核验，但没有 `metadata_cursor_committed=true`。\n'
                f'- 返回的是已有快照（source_count={coverage.get("source_count_including_discovered_children")}，{coverage.get("units_written_this_run")} units / {coverage.get("bytes_written_this_run")} bytes），不计入本批新增，也不推进 `{PRIOR_RUN}` 游标。\n'
                '- 根因分类为已复现的云端 worker timeout/非成功采集；semantic、策略、Canary 和实盘均保持 0。\n'
                '- 失败摘要已加密远端核验；后续只在去重后登记新的独立批次。\n'
            )
            path.write_text(text, encoding='utf-8')
    print(json.dumps({'status': publication.get('status'), 'batch': BATCH,
                      'run_id': RUN_ID, 'authoritative_cursor_run': PRIOR_RUN,
                      'cursor_advanced': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
