#!/usr/bin/env python3
"""Reconcile targeted extract-0171 without advancing the metadata cursor."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade')
BASE = ROOT / 'output/continuous-research-20261016'
STUDY = BASE / 'quantml-full-study'
READBACK = STUDY / 'extract-0171-readback-20261026.json'
RUN_ID = 40303497318
PREVIOUS_CURSOR_RUN = 40302264902


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'not_object:{path}')
    return value


def write(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> None:
    summary = load(READBACK)
    if summary.get('terminal_status') != 'success' or summary.get('metadata_cursor_committed') is not False:
        raise RuntimeError('targeted_readback_contract_failed')
    if (summary.get('sealed_journal') or {}).get('status') != 'verified_remote':
        raise RuntimeError('targeted_readback_not_sealed')
    coverage = summary.get('coverage') or {}
    proposal = summary.get('unit_delivery_proposal') or {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    action = (
        'Extract-0171 targeted recovery is readback-verified but did not commit the metadata cursor: '
        f'{coverage.get("units_written_this_run")} units / {coverage.get("bytes_written_this_run")} bytes, '
        'semantic credit remains 0. Preserve the 0170 cursor, review targeted source attribution, '
        'and do not repeat this recovery; only then consider one normal distinct cursor tranche.'
    )
    paths = [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json',
             STUDY / 'manifest-summary.json', STUDY / 'runtime-state.json', STUDY / 'progress.json']
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        if state.get('active_run') != RUN_ID or state.get('current_inflight_run') != RUN_ID:
            raise RuntimeError(f'checkpoint_identity_mismatch:{path.name}')
        state.update({
            'status': 'extract_0171_targeted_readback_verified_no_cursor_advance',
            'active_remote_task': None,
            'active_run': None,
            'current_inflight_run': None,
            'current_inflight_runs': [],
            'current_cloud_run_mode': 'extract',
            'current_cloud_run_status': 'completed_reconciled_targeted_no_cursor',
            'latest_quantml_extract_run': RUN_ID,
            'latest_quantml_extract_status': 'completed_success_targeted_recovery_no_cursor',
            'latest_quantml_extract_readback': str(READBACK),
            'latest_quantml_extract_units_written': coverage.get('units_written_this_run'),
            'latest_quantml_extract_bytes_written': coverage.get('bytes_written_this_run'),
            'latest_quantml_extract_evidence_count': proposal.get('evidence_count'),
            'latest_quantml_extract_cursor_committed': False,
            'latest_quantml_extract_previous_cursor_run': PREVIOUS_CURSOR_RUN,
            'next_action': action,
            'next_step': action,
            'quantml_next_action': action,
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
        # Keep the last committed cursor identity explicit; targeted recovery is not a cursor step.
        state['authoritative_cursor_run'] = PREVIOUS_CURSOR_RUN
        state['authoritative_cursor_status'] = 'committed_before_targeted_0171'
        write(path, state)
        changed += 1
    audit = {
        'format': 'quantml-extract-0171-targeted-reconciliation-v1',
        'created_utc': now,
        'readback': str(READBACK),
        'workflow_run_id': RUN_ID,
        'previous_committed_cursor_run': PREVIOUS_CURSOR_RUN,
        'metadata_cursor_committed': False,
        'source_count_including_discovered_children': coverage.get('source_count_including_discovered_children'),
        'units_written': coverage.get('units_written_this_run'),
        'bytes_written': coverage.get('bytes_written_this_run'),
        'transport_evidence_count': proposal.get('evidence_count'),
        'semantic_credit': 0,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'full_corpus_complete': False,
        'duplicate_dispatch': False,
        'cursor_advance_credited': False,
        'ack_registry_changed': False,
        'next_action': action,
        'state_files_updated': changed,
        'plaintext_published': False,
    }
    audit_path = STUDY / 'extract-0171-targeted-reconciliation-20261026.json'
    write(audit_path, audit)
    for path in [BASE / 'QuantML-学习进度.md', STUDY / 'QuantML-学习进度.md']:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        marker = 'EXTRACT-0171 定向恢复终态（不推进游标）（2026-10-26）'
        if marker not in text:
            text += (f'\n\n## {marker}\n\n'
                     f'- run `{RUN_ID}` 成功并完成 direct readback；定向恢复新增 {coverage.get("units_written_this_run")} units / {coverage.get("bytes_written_this_run")} bytes，source_count={coverage.get("source_count_including_discovered_children")}。\n'
                     '- 由于 `metadata_cursor_committed=false`，保留 0170 的已提交游标，不把定向恢复当作游标推进；ACK 未改变，semantic credit=0。\n'
                     '- 终态摘要已回读核验；不重复该定向恢复，下一步先归属审阅，再考虑一个普通的新游标批次。\n')
            path.write_text(text, encoding='utf-8')
    print(json.dumps(audit, ensure_ascii=False))


if __name__ == '__main__':
    main()
