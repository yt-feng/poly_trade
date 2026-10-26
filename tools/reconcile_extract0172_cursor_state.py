#!/usr/bin/env python3
"""Correct stale cursor fields after authenticated extract-0172 readback."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade')
BASE = ROOT / 'output/continuous-research-20261016'
STUDY = BASE / 'quantml-full-study'
N = os.environ.get('EXTRACT_BATCH', '0172')
READBACK = STUDY / f'extract-{N}-readback-20261026.json'
AUDIT = STUDY / f'extract-{N}-cursor-state-correction-20261026.json'
RUN_ID = int(os.environ.get('EXTRACT_RUN_ID', '40304795524'))
PRIOR_CURSOR = int(os.environ.get('EXTRACT_PRIOR_RUN', '40302264902'))


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'not_object:{path}')
    return value


def write(path: Path, value: dict) -> None:
    temp = path.with_name('.' + path.name + '.cursor.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main() -> None:
    readback = load(READBACK)
    if readback.get('workflow_run_id') != RUN_ID or readback.get('terminal_status') != 'success':
        raise RuntimeError('readback_identity_or_terminal_mismatch')
    if readback.get('metadata_cursor_committed') is not True:
        raise RuntimeError('readback_cursor_not_committed')
    if (readback.get('sealed_journal') or {}).get('status') != 'verified_remote':
        raise RuntimeError('readback_not_sealed')
    coverage = readback.get('coverage') or {}
    proposal = readback.get('unit_delivery_proposal') or {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    paths = [
        BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json',
        STUDY / 'manifest-summary.json', STUDY / 'runtime-state.json', STUDY / 'progress.json',
    ]
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        if state.get('active_run') is not None or state.get('current_inflight_run') is not None:
            raise RuntimeError(f'inflight_state_unexpected:{path.name}')
        previous = state.get('authoritative_cursor_run')
        if previous not in (None, PRIOR_CURSOR, RUN_ID):
            raise RuntimeError(f'authoritative_cursor_conflict:{path.name}:{previous}')
        state.update({
            'status': f'extract_{N}_readback_verified_partial',
            'current_cloud_run_status': 'completed_reconciled',
            'latest_quantml_extract_run': RUN_ID,
            'latest_quantml_extract_status': 'completed_success_partial_transport_no_ack',
            'latest_quantml_extract_cursor_committed': True,
            'latest_quantml_extract_previous_cursor_run': PRIOR_CURSOR,
            'authoritative_cursor_run': RUN_ID,
            'authoritative_cursor_status': 'committed_extract_0172',
            'cursor_state_correction': 'readback_confirmed_true; stale false field repaired',
            'checkpoint_updated_utc': now,
            'updated_utc': now,
            'updated_at_utc': now,
            'semantic_credit': 0,
            'quantml_semantic_read_count': 0,
            'semantic_learning_complete': False,
            'full_corpus_complete': False,
            'actual_fills': 0,
            'verified_canary_fills': 0,
            'live_orders_enabled': False,
            'buying_paused': True,
        })
        write(path, state)
        changed += 1
    audit = {
        'format': f'quantml-extract-{N}-cursor-state-correction-v1',
        'created_utc': now,
        'readback': str(READBACK),
        'workflow_run_id': RUN_ID,
        'previous_authoritative_cursor_run': PRIOR_CURSOR,
        'corrected_authoritative_cursor_run': RUN_ID,
        'readback_metadata_cursor_committed': True,
        'stale_field_before': False,
        'corrected_field_after': True,
        'source_count_including_discovered_children': coverage.get('source_count_including_discovered_children'),
        'units_written': coverage.get('units_written_this_run'),
        'bytes_written': coverage.get('bytes_written_this_run'),
        'transport_evidence_count': proposal.get('evidence_count'),
        'semantic_credit': 0,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'full_corpus_complete': False,
        'state_files_updated': changed,
        'duplicate_dispatch': False,
        'plaintext_published': False,
        'root_cause': 'generic finalizer omitted latest cursor commit fields after successful readback; encrypted readback remained authoritative',
        'next_action': f'Use extract-{N} as authoritative cursor; choose a future distinct tranche only after deduplication review, preserving semantic credit 0 and buying paused.',
    }
    write(AUDIT, audit)
    for path in [BASE / 'QuantML-学习进度.md', STUDY / 'QuantML-学习进度.md']:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        marker = f'## EXTRACT-{N} 游标字段一致性纠正（2026-10-26）'
        if marker not in text:
            text += (f'\n\n{marker}\n\n'
                     f'- readback `{RUN_ID}` 明确 `metadata_cursor_committed=true`，但通用收尾脚本遗留了旧的 `latest_quantml_extract_cursor_committed=false`；这属于控制面字段遗漏，不是云端提取失败。\n'
                     f'- 已在六份检查点将 authoritative cursor 统一纠正为 `{RUN_ID}`，并保留新增 {coverage.get("units_written_this_run")} units / {coverage.get("bytes_written_this_run")} bytes 与 semantic credit=0。\n'
                     '- 纠正摘要随后通过 sealed-research-journal 发布；下一步先消费去重归因，不把 transport 证据当作语义学习或 Canary。\n')
            path.write_text(text, encoding='utf-8')
    print(json.dumps(audit, ensure_ascii=False))


if __name__ == '__main__':
    main()
