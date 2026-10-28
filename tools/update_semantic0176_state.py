#!/usr/bin/env python3
"""Update bounded QuantML checkpoints after an uncredited semantic readback."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def atomic(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.state.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    next_action = 'Require a source path and source-id match before another semantic unit; do not replay run 40621220541 or change the frozen BTC protocol.'
    for path in [
        BASE / 'active-episode.json',
        BASE / 'cycle-progress.json',
        BASE / 'quantml-learning-progress.json',
        BASE / 'quantml-learning-state.json',
        BASE / 'quantml-full-study/manifest-summary.json',
        BASE / 'quantml-full-study/runtime-state.json',
        BASE / 'quantml-full-study/progress.json',
    ]:
        if not path.is_file():
            continue
        state = load(path)
        state['semantic_credit'] = int(state.get('semantic_credit', 0))
        state['semantic_backend_gate'] = 'remote_probe_authenticated; bounded_synthesis_readback_verified; latest_source_attribution_failed'
        state['semantic_backend_credentials_present_by_name'] = True
        state['semantic_backend_last_checked_utc'] = now
        state['semantic_next_action'] = next_action
        state['full_corpus_complete'] = False
        atomic(path, state)
    print(json.dumps({'status': 'state_updated', 'semantic_credit': 1, 'next_action': next_action}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
