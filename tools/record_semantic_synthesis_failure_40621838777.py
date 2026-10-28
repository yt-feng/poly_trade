#!/usr/bin/env python3
"""Seal the readback stale-queue defect observed in run 40621838777."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade')
BASE = ROOT / 'output/continuous-research-20261016'
TOOLS = ROOT / 'research-repair-20261015/tools'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'


def atomic(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    summary = {
        'format': 'quantml-semantic-synthesis-failure-v1',
        'created_utc': now,
        'run_id': '40621838777',
        'requested_source_id': '12f5799871017db2367870cbb9b81682faddf7d4f0d0d59c7d40d3e0e2d24e54',
        'observed_source_id': '095f61199323118a9c952831583a344760945c3a50d36429c57408b121cb5f60',
        'failure_class': 'readback_report_missing_requested_manifest_identity',
        'stage': 'readback_attribution',
        'semantic_credit': 0,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
        'next_action': 'Use readback manifest reconstruction from requested metadata; do not credit or replay run 40621838777.',
    }
    atomic(BASE / 'semantic-0178-readback-failure-20261028.json', summary)
    sys.path.insert(0, str(TOOLS))
    import research_vault as vault
    recipient = vault.load_json_bytes(vault.read_bounded(TOOLS.parent / 'research_vault/recipient.json', 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    delivery = BASE / 'semantic-0178-readback-failure-delivery'
    delivery.mkdir(mode=0o700, exist_ok=True)
    vault.write_new(delivery / 'part-0000.vault', vault.seal(vault.canonical(summary), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(delivery / 'status.json', vault.canonical({'version': 1, 'status': 'complete', 'stage': 'sealed_delivery', 'strategy_evaluated': False, 'live_orders_enabled': False, 'plaintext_published': False, 'sealed_parts': 1}) + b'\n', 0o644)
    pub = subprocess.run([
        sys.executable, str(TOOLS / 'publish_quality_first_journal.py'), '--source', str(delivery),
        '--repository', 'yt-feng/poly_trade', '--run-id', '40621838777', '--run-attempt', '1',
        '--recipient-id', RECIPIENT_ID, '--cell-id', 'semantic-0178-readback-failure-20261028',
    ], cwd=TOOLS.parent, capture_output=True, text=True, timeout=180, check=False)
    if pub.returncode:
        raise RuntimeError('journal_publish_failed')
    receipt = json.loads(pub.stdout)
    atomic(BASE / 'semantic-0178-readback-failure-publication.json', {
        'format': 'quantml-semantic-0178-failure-publication-v1', 'created_utc': now,
        'status': receipt.get('status'), 'remote_branch': receipt.get('branch'),
        'remote_episode_id': receipt.get('episode_id'), 'plaintext_published': False,
        'semantic_credit': 0, 'sealed_parts': receipt.get('sealed_parts'),
        'validation_scope': receipt.get('validation_scope'),
    })
    next_action = 'After readback manifest repair, use one new source cursor with source path and source-id match; do not replay runs 40621220541, 40621570188, or 40621838777.'
    for path in [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json', BASE / 'quantml-learning-state.json', BASE / 'quantml-full-study/manifest-summary.json', BASE / 'quantml-full-study/runtime-state.json', BASE / 'quantml-full-study/progress.json']:
        if path.is_file():
            state = json.loads(path.read_text(encoding='utf-8'))
            state['semantic_credit'] = int(state.get('semantic_credit', 0))
            state['semantic_backend_gate'] = 'remote_probe_authenticated; bounded_synthesis_readback_verified; readback_manifest_repair_pending_validation'
            state['semantic_backend_credentials_present_by_name'] = True
            state['semantic_backend_last_checked_utc'] = now
            state['semantic_next_action'] = next_action
            state['full_corpus_complete'] = False
            atomic(path, state)
    for path in [BASE / 'QuantML-学习进度.md', BASE / 'quantml-full-study/QuantML-学习进度.md']:
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            marker = '## semantic synthesis readback manifest repair（2026-10-28）'
            if marker not in text:
                path.write_text(text + '\n\n' + marker + '\n\n- run 40621838777 仍读回旧 source；该轮归类为 readback 报告缺少请求 manifest identity，未计语义学分。已修复 readback 侧从请求 source id 重建 bounded manifest。\n', encoding='utf-8')
    print(json.dumps({'status': 'sealed_failure', 'semantic_credit': 0, 'remote_episode_id': receipt.get('episode_id')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
