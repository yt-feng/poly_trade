#!/usr/bin/env python3
"""Synchronize the QuantML 0170 checkpoint with the authoritative run status."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
RUN_ID = 40302264902


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'not_object:{path}')
    return value


def write(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
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
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    action = (
        f'Extract-0170 remains the single in-flight QuantML continuation (run {RUN_ID}); '
        'read its terminal status and encrypted direct readback before any new dispatch. '
        'Keep semantic credit 0, BTC training frozen, and buying paused.'
    )
    paths = [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json',
             STUDY / 'manifest-summary.json', STUDY / 'runtime-state.json', STUDY / 'progress.json']
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        if state.get('active_run') != RUN_ID or state.get('current_inflight_run') != RUN_ID:
            raise SystemExit(f'checkpoint_identity_mismatch:{path.name}')
        state.update({
            'current_cloud_run_status': run.get('status'),
            'active_remote_task': dict(state.get('active_remote_task') or {}, remote_run=run),
            'next_action': action,
            'next_step': action,
            'quantml_next_action': action,
            'checkpoint_updated_utc': now,
            'updated_utc': now,
            'updated_at_utc': now,
        })
        write(path, state)
        changed += 1
    audit = BASE / 'extract-0170-inflight-reconciliation-20261026.json'
    write(audit, {
        'format': 'quantml-extract-0170-inflight-reconciliation-v1',
        'created_at_utc': now,
        'run': run,
        'state_files_updated': changed,
        'terminal': False,
        'semantic_credit': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'next_action': action,
        'plaintext_published': False,
    })
    print(json.dumps({'status': 'reconciled', 'run_id': RUN_ID, 'run_status': run.get('status'), 'states_updated': changed}, ensure_ascii=False))


if __name__ == '__main__':
    main()
