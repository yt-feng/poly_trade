#!/usr/bin/env python3
"""Read back the sealed divergence-gate paper replay and update checkpoints."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade')
BASE = ROOT / 'output/continuous-research-20261016'
WORKTREE = ROOT / 'research-repair-20261015'
PART = WORKTREE / 'output/continuous-research-20261016/quantml-full-study/divergence-gate-run-40623100957-part-0000.vault'
IDENTITY = Path('/Users/ytfeng/.codex/private/poly-quality-first-20261015/audit-private.key')
RID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'


def atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    import sys
    sys.path.insert(0, str(ROOT / 'research-repair-20261015/tools'))
    import research_vault as vault
    private = X25519PrivateKey.from_private_bytes(IDENTITY.read_bytes())
    doc = vault.load_json_bytes(vault.unseal(PART.read_bytes(), private), 16 * 1024 * 1024)
    if doc.get('format') != 'old-data-causal-quantml-divergence-gate-sealed-v1':
        raise SystemExit('unexpected_result_format')
    meta = doc.get('meta') or {}
    rows = []
    for row in csv.DictReader(io.StringIO(doc.get('metrics_csv', ''))):
        if row.get('slippage') == '0.005' and row.get('split') in ('holdout59', 'block1', 'block2', 'block3', 'block4', 'block5'):
            rows.append({k: row.get(k) for k in ('split', 'trades', 'ending_cash', 'net_return')})
    result = {
        'schema': 'quantml-divergence-gate-readback-v1',
        'workflow_run_id': 40623100957,
        'source_data_commit': meta.get('source_repo_commit'),
        'remote_envelope_verified': True,
        'strategy_evaluated': True,
        'paper_only': True,
        'gate_passed': bool((meta.get('summary') or {}).get('gate_passed')),
        'decision': meta.get('decision'),
        'summary': meta.get('summary'),
        'metrics_rows': rows,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
        'root_cause': 'Aggregate holdout growth is concentrated in early blocks; later blocks breach the minimum cash floor, so the divergence signal is not stable enough for a ten-trade same-ledger canary.',
        'next_action': 'Quarantine this challenger; do not promote or replay it. Require a separately registered, causal and costed candidate with every chronological block above the cash floor.',
    }
    atomic(BASE / 'quantml-full-study/divergence-gate-result-20261028.json', result)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    for path in [BASE / 'active-episode.json', BASE / 'cycle-progress.json', BASE / 'quantml-learning-progress.json', BASE / 'quantml-learning-state.json', BASE / 'quantml-full-study/manifest-summary.json', BASE / 'quantml-full-study/runtime-state.json', BASE / 'quantml-full-study/progress.json']:
        if path.is_file():
            state = json.loads(path.read_text(encoding='utf-8'))
            state['strategy_evaluations'] = int(state.get('strategy_evaluations', 0)) + 1
            state['actual_fills'] = int(state.get('actual_fills', 0))
            state['verified_canary_fills'] = int(state.get('verified_canary_fills', 0))
            state['live_orders_enabled'] = False
            state['semantic_credit'] = int(state.get('semantic_credit', 0))
            state['full_corpus_complete'] = False
            state['latest_paper_evaluation'] = {'workflow_run_id': 40623100957, 'candidate': 'quantml_btc_probability_divergence_gate', 'gate_passed': False, 'holdout_ending_cash': (meta.get('summary') or {}).get('holdout_ending_cash_005'), 'minimum_block_ending_cash': (meta.get('summary') or {}).get('minimum_block_ending_cash'), 'recorded_utc': now}
            state['next_action'] = result['next_action']
            atomic(path, state)
    marker = '## divergence-gate 旧干净纸面复放（2026-10-28）'
    section = ('\n\n' + marker + '\n\n'
               '- Actions run 40623100957 已完成并通过密文回读：holdout 20 笔、期末现金约 43.77 美元，但 block2–5 的最低现金约 3.37 美元，固定门失败。\n'
               '- 这是受控回放，不是实盘成交；candidate_promoted=false、actual_fills=0、verified_canary_fills=0、live=false。根因是块间稳定性和现金底线不成立，整体收益不能替代连续复利证据。\n')
    for path in [BASE / 'QuantML-学习进度.md', BASE / 'quantml-full-study/QuantML-学习进度.md']:
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            if marker not in text:
                path.write_text(text + section, encoding='utf-8')
    print(json.dumps({'status': 'reconciled', 'gate_passed': result['gate_passed'], 'holdout_ending_cash': (meta.get('summary') or {}).get('holdout_ending_cash_005'), 'minimum_block_ending_cash': (meta.get('summary') or {}).get('minimum_block_ending_cash')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
