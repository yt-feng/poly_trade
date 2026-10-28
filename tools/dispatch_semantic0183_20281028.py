#!/usr/bin/env python3
"""Dispatch one distinct, bounded DeepSeek semantic cursor and register its run."""
from __future__ import annotations
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade')
WORKTREE = ROOT / 'research-repair-20261015'
BASE = ROOT / 'output/continuous-research-20261016'
STUDY = BASE / 'quantml-full-study'
RID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
BATCH = os.environ.get('SEMANTIC_BATCH', '0183')
SOURCE_ID = os.environ.get('SEMANTIC_SOURCE_ID', 'c299c322e48f404f956d72bbc32029c6ab0a8d5552e001350a23ff5165b80666')
SOURCE_PATH = os.environ.get('SEMANTIC_SOURCE_PATH', '策略复现（持续更新）/70-发薪日异常/result/绩效.md')
PREFLIGHT = STUDY / f'semantic-{BATCH}-preflight-20281028.json'
DELIVERY = STUDY / f'semantic-{BATCH}-preflight-delivery'
DISPATCH = STUDY / f'semantic-{BATCH}-dispatch-20281028.json'
RECEIPT = STUDY / f'semantic-{BATCH}-preflight-publication-20281028.json'

sys.path.insert(0, str(WORKTREE / 'tools'))
import research_vault as vault  # noqa: E402


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'not_object:{path}')
    return value


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def gh_runs() -> list[dict]:
    command = ['gh', 'run', 'list', '--repo', 'yt-feng/poly_trade', '--workflow', 'quantml-semantic-synthesis.yml', '--limit', '20', '--json', 'databaseId,status,conclusion,headSha,createdAt,event']
    last_error = ''
    for delay in (0, 2, 4):
        if delay:
            import time
            time.sleep(delay)
        proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45, check=False)
        if proc.returncode == 0:
            value = json.loads(proc.stdout)
            return value if isinstance(value, list) else []
        last_error = proc.stderr[-300:]
    # Fall back to the REST list after transient gh CLI EOFs; this is read-only
    # and preserves the dispatch-before-readback idempotency gate.
    proc = subprocess.run(['gh', 'api', 'repos/yt-feng/poly_trade/actions/runs?event=workflow_dispatch&per_page=30'], cwd=ROOT, capture_output=True, text=True, timeout=45, check=False)
    if proc.returncode == 0:
        payload = json.loads(proc.stdout)
        runs = payload.get('workflow_runs', []) if isinstance(payload, dict) else []
        return [{
            'databaseId': item.get('id'), 'status': item.get('status'), 'conclusion': item.get('conclusion'),
            'headSha': item.get('head_sha'), 'createdAt': item.get('created_at'), 'event': item.get('event'),
        } for item in runs if item.get('name') == 'quantml-semantic-synthesis']
    raise RuntimeError(f'github_list_failed:{last_error or proc.stderr[-300:]}')


def main() -> None:
    state = read(STUDY / 'progress.json')
    if state.get('quantml_inflight_extract_run') or state.get('semantic_inflight_run'):
        raise SystemExit('inflight_exists')
    if PREFLIGHT.exists() or DELIVERY.exists():
        raise SystemExit('already_exists')
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now = before.isoformat().replace('+00:00', 'Z')
    preflight = {
        'format': f'quantml-semantic-{BATCH}-preflight-v1',
        'created_utc': now,
        'workflow': 'quantml-semantic-synthesis.yml',
        'source_scope': 'one explicit registered source cursor; bounded DeepSeek synthesis; not full corpus',
        'source_id': SOURCE_ID,
        'source_path': SOURCE_PATH,
        'parameters': {'recovery_source_id': SOURCE_ID, 'max_metadata': 1, 'max_files': 1, 'max_units': 2, 'pdf_ocr': False, 'model': 'deepseek-v4-pro', 'temperature': 0, 'max_tokens': 4800},
        'gates': {'duplicate_dispatch': False, 'semantic_credit_before': state.get('semantic_credit'), 'full_corpus_complete_before': state.get('full_corpus_complete'), 'actual_fills_before': state.get('actual_fills'), 'verified_canary_before': state.get('verified_canary_fills'), 'buying_paused': state.get('buying_paused', True)},
        'hypothesis': 'A newly selected, previously unattempted source cursor can produce an attributable strict-JSON semantic card after prior source-attribution and timeout failures.',
        'success_contract': 'HTTP 200, strict schema, one source path, every locator in scope; only then credit one semantic unit.',
        'failure_contract': 'Seal and classify failure; semantic credit remains unchanged and the frozen BTC protocol is not touched.',
        'plaintext_published': False,
    }
    write(PREFLIGHT, preflight)
    recipient = vault.load_json_bytes(vault.read_bounded(WORKTREE / 'research_vault/recipient.json', 4096), 4096)
    vault.validate_recipient(recipient, RID)
    DELIVERY.mkdir(mode=0o700)
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({'format': preflight['format'], 'preflight': preflight, 'plaintext_published': False}), recipient, RID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({'version': 1, 'status': 'complete', 'stage': 'sealed_delivery', 'strategy_evaluated': False, 'live_orders_enabled': False, 'plaintext_published': False, 'sealed_parts': 1}) + b'\n', 0o644)

    dispatch_proc = subprocess.run(['gh', 'workflow', 'run', 'quantml-semantic-synthesis.yml', '--repo', 'yt-feng/poly_trade', '--ref', 'main', '-f', f'recovery_source_id={SOURCE_ID}'], cwd=ROOT, capture_output=True, text=True, timeout=45, check=False)
    if dispatch_proc.returncode:
        raise RuntimeError(f'dispatch_failed:{dispatch_proc.stderr[-500:]}')
    candidates = []
    for run in gh_runs():
        if run.get('event') != 'workflow_dispatch':
            continue
        created = run.get('createdAt', '')
        if created and created >= before.isoformat().replace('+00:00', 'Z'):
            candidates.append(run)
    if len(candidates) != 1:
        raise RuntimeError(f'dispatch_outcome_ambiguous:{candidates}')
    run = candidates[0]
    run_id = int(run['databaseId'])
    action = f'Semantic-{BATCH} is the single in-flight DeepSeek cursor (run {run_id}); read terminal sealed output before any new semantic dispatch. Keep credit unchanged until strict source-locator verification.'
    dispatch = {'format': f'quantml-semantic-{BATCH}-dispatch-v1', 'created_utc': now, 'workflow': 'quantml-semantic-synthesis.yml', 'run_id': run_id, 'run_attempt': 1, 'source_id': SOURCE_ID, 'source_path': SOURCE_PATH, 'preflight': str(PREFLIGHT), 'duplicate_dispatch': False, 'no_strategy_evaluation': True, 'no_model_refit': True, 'no_live_orders': True, 'semantic_credit_before': state.get('semantic_credit'), 'actual_fills_before': state.get('actual_fills'), 'verified_canary_before': state.get('verified_canary_fills'), 'live_orders_enabled': False, 'remote': run, 'plaintext_published': False}
    write(DISPATCH, dispatch)
    paths = [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json', BASE / 'quantml-learning-state.json', STUDY / 'manifest-summary.json', STUDY / 'runtime-state.json', STUDY / 'progress.json']
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        st = read(path)
        st.update({'semantic_inflight_run': run_id, 'semantic_inflight_status': f'semantic_{BATCH}_in_progress', 'active_run': run_id, 'active_run_kind': 'quantml_semantic', 'current_inflight_run': run_id, 'current_inflight_runs': [run_id], 'current_cloud_execution': 'in_progress', 'current_cloud_run_status': run.get('status'), 'active_remote_task': {'remote_run': run, 'kind': 'quantml_semantic', 'batch': BATCH, 'source_id': SOURCE_ID}, 'semantic_next_action': action, 'quantml_next_action': action, 'next_action': action, 'next_step': action, 'checkpoint_updated_utc': now, 'updated_utc': now, 'updated_at_utc': now})
        write(path, st)
        changed += 1
    pub_proc = subprocess.run([sys.executable, str(WORKTREE / 'tools/publish_quality_first_journal.py'), '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade', '--run-id', str(run_id), '--run-attempt', '1', '--recipient-id', RID, '--cell-id', f'semantic{BATCH}-preflight'], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if pub_proc.returncode:
        raise RuntimeError(f'publication_failed:{pub_proc.stderr[-500:]}')
    publication = json.loads(pub_proc.stdout.strip().splitlines()[-1])
    write(RECEIPT, {'run_id': run_id, 'publication': publication})
    print(json.dumps({'status': 'registered', 'run_id': run_id, 'run_status': run.get('status'), 'states_updated': changed, 'publication_status': publication.get('status')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
