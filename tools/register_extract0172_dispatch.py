#!/usr/bin/env python3
"""Record one already-dispatched normal extract-0172 run after remote readback."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade')
BASE = ROOT / 'output/continuous-research-20261016'
STUDY = BASE / 'quantml-full-study'
N = os.environ.get('EXTRACT_BATCH', '0172')
RUN_ID = int(os.environ['EXTRACT_RUN_ID'])
PREFLIGHT = STUDY / f'extract-{N}-preflight-20261026.json'
DISPATCH = STUDY / f'extract-{N}-dispatch-20261026.json'
DELIVERY = STUDY / f'extract-{N}-dispatch-delivery'
RECEIPT = STUDY / f'extract-{N}-dispatch-publication.json'
RECIPIENT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015/research_vault/recipient.json')
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
CELL_ID = f'quantml-extract-{N}-dispatch'
PRIOR_RUN = int(os.environ.get('EXTRACT_PRIOR_RUN', '40302264902'))

sys.path.insert(0, '/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015/tools')
import research_vault as vault  # noqa: E402


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'not_object:{path}')
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
        if preflight.get('source_previous_batch', {}).get('run_id') != PRIOR_RUN:
            raise SystemExit('prior_cursor_identity_conflict')
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        dispatch = {
            'format': f'quantml-extract-{N}-dispatch-v1',
            'created_utc': now,
            'workflow': 'corpus-private-extract.yml',
            'run_id': RUN_ID,
            'run_attempt': 1,
            'mode': 'extract',
            'parameters': preflight.get('parameters'),
            'preflight': str(PREFLIGHT),
            'preflight_publication': str(STUDY / f'extract-{N}-preflight-publication-20261026.json'),
            'duplicate_dispatch': False,
            'targeted_recovery': False,
            'prior_committed_cursor_run': PRIOR_RUN,
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
        f'Extract-{N} is the single registered in-flight normal cursor continuation (run {RUN_ID}); '
        f'poll and read it back before any further dispatch. It follows committed cursor run {PRIOR_RUN}, '
        'does not replay targeted recovery, keeps semantic credit 0, BTC training frozen and buying paused.'
    )
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    paths = [
        BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json',
        BASE / 'quantml-learning-state.json',
        STUDY / 'manifest-summary.json', STUDY / 'runtime-state.json', STUDY / 'progress.json',
    ]
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        state.update({
            'status': f'extract_{N}_inflight',
            'active_remote_task': dispatch,
            'active_run': RUN_ID,
            'current_inflight_run': RUN_ID,
            'current_inflight_runs': [RUN_ID],
            'current_cloud_run_mode': 'extract',
            'current_cloud_run_status': run.get('status'),
            'latest_quantml_dispatch': str(DISPATCH),
            'latest_quantml_preflight': str(PREFLIGHT),
            'latest_quantml_extract_run': RUN_ID,
            'latest_quantml_extract_status': 'in_progress',
            'authoritative_cursor_run': PRIOR_RUN,
            'next_action': action,
            'next_step': action,
            'quantml_next_action': action,
            'checkpoint_updated_utc': now,
            'updated_utc': now,
            'updated_at_utc': now,
            'semantic_credit': 0,
            'quantml_semantic_read_count': 0,
            'full_corpus_complete': False,
            'semantic_learning_complete': False,
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
        # A prior attempt may have created the delivery files before a
        # transient publisher failure.  Keep the already-created bytes and
        # resume publication instead of raising FileExistsError or resealing
        # the same dispatch.
        sealed_part = DELIVERY / 'part-0000.vault'
        if not sealed_part.exists():
            vault.write_new(sealed_part, vault.seal(vault.canonical({
                'format': f'quantml-extract-{N}-dispatch-sealed-v1',
                'dispatch': dispatch,
                'plaintext_published': False,
                'actual_fills': 0,
                'verified_canary_fills': 0,
                'live_orders_enabled': False,
            }), recipient, RECIPIENT_ID), 0o600)
        status_file = DELIVERY / 'status.json'
        if not status_file.exists():
            vault.write_new(status_file, vault.canonical({
                'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
                'strategy_evaluated': False, 'live_orders_enabled': False,
                'plaintext_published': False, 'sealed_parts': 1,
            }) + b'\n', 0o644)
        proc = subprocess.run([
            sys.executable, str(ROOT / 'research-repair-20261015/tools/publish_quality_first_journal.py'),
            '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
            '--run-id', str(RUN_ID), '--run-attempt', '1',
            '--recipient-id', RECIPIENT_ID, '--cell-id', CELL_ID,
        ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
        if proc.returncode != 0:
            raise SystemExit(proc.stdout.strip() or 'dispatch_publication_failed')
        publication = json.loads(proc.stdout)
        write_json(RECEIPT, {
            'format': f'quantml-extract-{N}-dispatch-publication-v1',
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
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            marker = f'## EXTRACT-{N} 云端普通续批登记（2026-10-26）'
            if marker not in text:
                params = dispatch.get('parameters') or {}
                path.write_text(text + f'\n\n{marker}\n\n'
                    f'- 在前一 authoritative run `{PRIOR_RUN}` 已提交游标、此前定向恢复已归因且无游标推进后，只登记一次普通新游标 run `{RUN_ID}`（{params.get("max_metadata")} metadata / {params.get("max_files")} files / {params.get("max_units")} units）。\n'
                    f'- 不带定向 source id，不重复此前恢复；该批只推进 QuantML 覆盖，不训练、不评估 BTC 策略、不计 semantic credit 或 Canary。\n'
                    '- 预检、派发和状态登记均通过密文流程核验；终态回读前不重复派发，实盘买入继续暂停。\n', encoding='utf-8')
    print(json.dumps({'status': 'registered', 'batch': N, 'run_id': RUN_ID, 'run_status': run.get('status'), 'dispatch': str(DISPATCH)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
