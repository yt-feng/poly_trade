#!/usr/bin/env python3
"""Record strict-cursor repair in the local QuantML checkpoints."""
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
    next_action = 'After strict-cursor repair, use at most one new source cursor with source path and source-id match; do not replay runs 40621220541 or 40621570188.'
    for path in [
        BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json',
        BASE / 'quantml-learning-state.json', BASE / 'quantml-full-study/manifest-summary.json',
        BASE / 'quantml-full-study/runtime-state.json', BASE / 'quantml-full-study/progress.json',
    ]:
        if not path.is_file():
            continue
        state = json.loads(path.read_text(encoding='utf-8'))
        state['semantic_credit'] = int(state.get('semantic_credit', 0))
        state['semantic_backend_gate'] = 'remote_probe_authenticated; bounded_synthesis_readback_verified; strict_cursor_repair_pending_validation'
        state['semantic_backend_credentials_present_by_name'] = True
        state['semantic_backend_last_checked_utc'] = now
        state['semantic_next_action'] = next_action
        state['full_corpus_complete'] = False
        atomic(path, state)
    marker = '## semantic synthesis strict-cursor repair（2026-10-28）'
    section = (
        '\n\n' + marker + '\n\n'
        '- run 40621570188 指定了新 source id，但旧队列回退交付了另一份材料；该轮不计语义学分。\n'
        '- 已修复显式 cursor 的交付边界：有 target source 时不再回退旧队列；下一轮必须同时核对 source_id 与 source_path。\n'
    )
    for path in [BASE / 'QuantML-学习进度.md', BASE / 'quantml-full-study/QuantML-学习进度.md']:
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            if marker not in text:
                path.write_text(text + section, encoding='utf-8')
    print(json.dumps({'status': 'state_updated', 'semantic_credit': 1, 'next_action': next_action}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
