#!/usr/bin/env python3
"""Seal the 2026-10-28 semantic readback attribution failure."""
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    summary = {
        'format': 'quantml-semantic-synthesis-failure-v1',
        'created_utc': now,
        'run_id': '40621220541',
        'failure_class': 'source_cursor_paths_unknown_or_non_unique',
        'stage': 'readback_attribution',
        'http_status': 200,
        'model': 'deepseek-v4-pro',
        'source_text_count_sent': 2,
        'semantic_credit': 0,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
        'next_action': 'Do not credit this readback or replay the cursor; require a source path and source-id match before another semantic unit.',
    }
    atomic(BASE / 'semantic-0176-readback-failure-20261028.json', summary)
    sys.path.insert(0, str(TOOLS))
    import research_vault as vault

    recipient = vault.load_json_bytes(vault.read_bounded(TOOLS.parent / 'research_vault/recipient.json', 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    delivery = BASE / 'semantic-0176-readback-failure-delivery'
    delivery.mkdir(mode=0o700, exist_ok=True)
    vault.write_new(delivery / 'part-0000.vault', vault.seal(vault.canonical(summary), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(
        delivery / 'status.json',
        vault.canonical({'version': 1, 'status': 'complete', 'stage': 'sealed_delivery', 'strategy_evaluated': False, 'live_orders_enabled': False, 'plaintext_published': False, 'sealed_parts': 1}) + b'\n',
        0o644,
    )
    pub = subprocess.run(
        [
            sys.executable,
            str(TOOLS / 'publish_quality_first_journal.py'),
            '--source', str(delivery),
            '--repository', 'yt-feng/poly_trade',
            '--run-id', '40621220541',
            '--run-attempt', '1',
            '--recipient-id', RECIPIENT_ID,
            '--cell-id', 'semantic-0176-readback-failure-20261028',
        ],
        cwd=TOOLS.parent,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if pub.returncode:
        raise RuntimeError('journal_publish_failed')
    receipt = json.loads(pub.stdout)
    atomic(BASE / 'semantic-0176-readback-failure-publication.json', {
        'format': 'quantml-semantic-0176-failure-publication-v1',
        'created_utc': now,
        'status': receipt.get('status'),
        'remote_branch': receipt.get('branch'),
        'remote_episode_id': receipt.get('episode_id'),
        'plaintext_published': False,
        'semantic_credit': 0,
        'sealed_parts': receipt.get('sealed_parts'),
        'validation_scope': receipt.get('validation_scope'),
    })
    for path in [BASE / 'QuantML-学习进度.md', BASE / 'quantml-full-study/QuantML-学习进度.md']:
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            marker = '## semantic synthesis source attribution failure（2026-10-28）'
            if marker not in text:
                text += (
                    '\n\n' + marker + '\n\n'
                    '- Actions run 40621220541 的 DeepSeek v4-pro 返回 HTTP 200，但 readback 的两个 source_paths 均为 unknown，无法与唯一登记材料建立归属。\n'
                    '- 本轮不计 semantic credit、不登记假设、不做策略评估；实际成交、canary 和 live 仍为 0/false。失败摘要已用公钥密文封存。\n'
                )
                path.write_text(text, encoding='utf-8')
    print(json.dumps({'status': 'sealed_failure', 'semantic_credit': 0, 'remote_episode_id': receipt.get('episode_id')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
