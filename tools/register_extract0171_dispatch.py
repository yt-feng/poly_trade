#!/usr/bin/env python3
"""Register and seal the single targeted extract-0171 recovery run."""
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
RUN_ID = 40303497318
PREFLIGHT = STUDY / 'extract-0171-preflight-20261026.json'
DISPATCH = STUDY / 'extract-0171-dispatch-20261026.json'
DELIVERY = STUDY / 'extract-0171-dispatch-delivery'
RECEIPT = STUDY / 'extract-0171-dispatch-publication.json'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
CELL_ID = 'quantml-extract-0171-dispatch'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'not_object:{path}')
    return value


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def verify_run() -> dict:
    proc = subprocess.run([
        'gh', 'run', 'view', str(RUN_ID), '--repo', 'yt-feng/poly_trade',
        '--json', 'databaseId,status,conclusion,workflowName,event,headSha',
    ], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise SystemExit('github_run_read_failed')
    run = json.loads(proc.stdout)
    if run.get('workflowName') != 'corpus-private-extract' or run.get('event') != 'workflow_dispatch':
        raise SystemExit('unexpected_run_identity')
    if run.get('status') not in {'queued', 'in_progress'}:
        raise SystemExit(f'run_not_inflight:{run.get("status")}:{run.get("conclusion")}')
    return run


def main() -> None:
    run = verify_run()
    if DISPATCH.exists():
        dispatch = load(DISPATCH)
        if dispatch.get('run_id') != RUN_ID:
            raise SystemExit('dispatch_identity_conflict')
    else:
        preflight = load(PREFLIGHT)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        dispatch = {
            'format': 'quantml-extract-0171-dispatch-v1',
            'created_utc': now,
            'workflow': 'corpus-private-extract.yml',
            'run_id': RUN_ID,
            'run_attempt': 1,
            'mode': 'extract',
            'parameters': preflight['parameters'],
            'preflight': str(PREFLIGHT),
            'preflight_publication': str(STUDY / 'extract-0171-preflight-publication.json'),
            'duplicate_dispatch': False,
            'targeted_recovery': True,
            'no_strategy_evaluation': True,
            'no_model_refit': True,
            'no_live_orders': True,
            'semantic_credit_before': 0,
            'actual_fills_before': 0,
            'verified_canary_before': 0,
            'live_orders_enabled': False,
            'remote_run_observed': run,
        }
        write_json(DISPATCH, dispatch)
    action = (
        f'Extract-0171 is the single registered in-flight targeted recovery (run {RUN_ID}); '
        'poll/read back it before any new dispatch. It targets only the pending homework PDF '
        'coverage; semantic credit remains 0, BTC training is frozen, and buying stays paused.'
    )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    paths = [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json',
             STUDY / 'manifest-summary.json', STUDY / 'runtime-state.json', STUDY / 'progress.json']
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        state.update({
            'status': 'extract_0171_inflight',
            'active_remote_task': dispatch,
            'active_run': RUN_ID,
            'current_inflight_run': RUN_ID,
            'current_inflight_runs': [RUN_ID],
            'current_cloud_run_mode': 'extract',
            'current_cloud_run_status': run.get('status'),
            'latest_quantml_dispatch': str(DISPATCH),
            'latest_quantml_preflight': str(PREFLIGHT),
            'next_action': action,
            'next_step': action,
            'quantml_next_action': action,
            'semantic_credit': 0,
            'quantml_semantic_read_count': 0,
            'full_corpus_complete': False,
            'checkpoint_updated_utc': now,
            'updated_utc': now,
            'updated_at_utc': now,
            'actual_fills': 0,
            'verified_canary_fills': 0,
            'live_orders_enabled': False,
            'buying_paused': True,
        })
        write_json(path, state)
    if not RECEIPT.exists():
        recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
        vault.validate_recipient(recipient, RECIPIENT_ID)
        DELIVERY.mkdir(mode=0o700, exist_ok=True)
        vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({
            'format': 'quantml-extract-0171-dispatch-sealed-v1',
            'dispatch': dispatch,
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
            '--run-id', str(RUN_ID), '--run-attempt', '1',
            '--recipient-id', RECIPIENT_ID, '--cell-id', CELL_ID,
        ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
        if proc.returncode != 0:
            raise SystemExit(proc.stdout.strip() or 'dispatch_publication_failed')
        publication = json.loads(proc.stdout)
        write_json(RECEIPT, {
            'format': 'quantml-extract-0171-dispatch-publication-v1',
            'cell_id': CELL_ID,
            'source': str(DISPATCH),
            'source_sha256': hashlib.sha256(DISPATCH.read_bytes()).hexdigest(),
            'publisher_run_id': str(RUN_ID),
            'publisher_run_attempt': 1,
            'recipient_id': RECIPIENT_ID,
            'publication': publication,
            'plaintext_published': False,
        })
    for path in [BASE / 'QuantML-学习进度.md', STUDY / 'QuantML-学习进度.md']:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        marker = 'EXTRACT-0171 定向云端恢复登记（2026-10-26）'
        if marker not in text:
            text += (f'\n\n## {marker}\n\n'
                     f'- 基于 0170 来源归属审阅，只登记一次针对未完成作业 PDF 的定向恢复 run `{RUN_ID}`，目标 source id 固定且不扩展到已完成尾部相关性 PDF。\n'
                     '- 该批只推进页/视觉覆盖，不训练、不评估 BTC 策略、不计 semantic credit 或 Canary；终态回读前不重复派发。\n'
                     f'- 预检与派发密文回执已远端核验；当前状态 `{run.get("status")}`，`actual fills=0`、`verified Canary=0/10`、实盘买入暂停。\n')
            path.write_text(text, encoding='utf-8')
    print(json.dumps({'status': 'registered', 'run_id': RUN_ID, 'run_status': run.get('status'), 'dispatch': str(DISPATCH)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
