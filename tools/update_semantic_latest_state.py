#!/usr/bin/env python3
"""Reconcile local checkpoints after the verified source-attribution run."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')


def atomic(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    next_action = 'Source attribution is repaired and latest semantic API timeout is recorded; proceed with the in-flight divergence-gate paper replay, keep semantic credit at 1, and do not replay timed-out semantic cursors.'
    for path in [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json', BASE / 'quantml-learning-state.json', BASE / 'quantml-full-study/manifest-summary.json', BASE / 'quantml-full-study/runtime-state.json', BASE / 'quantml-full-study/progress.json']:
        if path.is_file():
            state = json.loads(path.read_text(encoding='utf-8'))
            state['semantic_credit'] = int(state.get('semantic_credit', 0))
            state['semantic_backend_gate'] = 'remote_probe_authenticated; source_attribution_verified; semantic_api_timeout_observed'
            state['semantic_backend_credentials_present_by_name'] = True
            state['semantic_backend_last_checked_utc'] = now
            state['semantic_next_action'] = next_action
            state['full_corpus_complete'] = False
            atomic(path, state)
    marker = '## semantic synthesis source attribution verified（2026-10-28）'
    section = '\n\n' + marker + '\n\n- run 40622708920 已核验 source_id 与完整路径一致，DeepSeek 随后超时；未计 semantic credit。来源归属修复已验证，当前转向在途的云端 divergence-gate 纸面复放。\n'
    for path in [BASE / 'QuantML-学习进度.md', BASE / 'quantml-full-study/QuantML-学习进度.md']:
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            if marker not in text:
                path.write_text(text + section, encoding='utf-8')
    print(json.dumps({'status': 'state_reconciled', 'semantic_credit': 1, 'next_action': next_action}, ensure_ascii=False))


if __name__ == '__main__':
    main()
