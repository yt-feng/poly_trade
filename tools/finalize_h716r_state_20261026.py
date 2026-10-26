#!/usr/bin/env python3
"""Close H716R after authenticated sealed-result readback."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
PRIVATE_RESULT = Path('/Users/ytfeng/.codex/private/poly-quality-first-20261015/H716R-result-40299229883.json')
RUN_ID = '40299229883'
REMOTE_EPISODE = '56be280b3f76d67ca15ff37a18a9bbbd104be01c3374564b52326c7e576dc096'
FILES = [BASE/'active-episode.json', BASE/'cycle-progress.json', STUDY/'manifest-summary.json', STUDY/'runtime-state.json', STUDY/'progress.json']


def main() -> None:
    payload = json.loads(PRIVATE_RESULT.read_text(encoding='utf-8'))
    summary = payload['summary']
    if summary.get('hypothesis_id') != 'H716R_H30_SERIAL_EXECUTION_CLOCK_REPAIR':
        raise ValueError('unexpected_h716r_hypothesis')
    metrics = list(csv.DictReader(io.StringIO(payload['metrics_csv'])))
    selection = [r for r in metrics if r['split'] == 'selection20']
    holdout = [r for r in metrics if r['split'] == 'holdout59']
    blocks = [r for r in metrics if r['split'].startswith('holdout_block_')]
    block_pass = {
        r['split']: all(r[key].lower() == 'true' for key in ('first_ten_passed',))
        and int(r['first_ten_fills']) == 10
        for r in blocks
    }
    compact = {
        'format': 'h716r-authenticated-result-compact-v1',
        'hypothesis_id': summary['hypothesis_id'],
        'parent_hypothesis_id': summary['parent_hypothesis_id'],
        'run_id': RUN_ID,
        'remote_episode_id': REMOTE_EPISODE,
        'result_now_authenticated_and_read': True,
        'decision': summary['decision'],
        'selection_prefix_passed': bool(summary['selection_prefix_passed']),
        'holdout_prefix_passed': bool(summary['holdout_prefix_passed']),
        'selection': [
            {k: r[k] for k in ('slippage', 'paper_trades', 'paper_ending_cash', 'first_ten_fills', 'first_ten_ending_cash', 'first_ten_minimum_cash', 'first_ten_failure_reason')}
            for r in selection
        ],
        'holdout': [
            {k: r[k] for k in ('slippage', 'paper_trades', 'paper_ending_cash', 'first_ten_fills', 'first_ten_ending_cash', 'first_ten_minimum_cash', 'first_ten_failure_reason')}
            for r in holdout
        ],
        'holdout_block_pass': block_pass,
        'root_cause_classification': 'implementation_defect_fixed_but_strategy_path_not_stable',
        'root_cause': 'Execution-clock and overlapping-ledger defects were repaired; the frozen H716 edge-cap path still fails both selection stress points and three of five chronological holdout blocks, so it cannot support a robust ten-fill compound canary.',
        'new_data_collected': False,
        'model_refit': False,
        'live_orders_enabled': False,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'paper_only': True,
    }
    out = STUDY / 'h716r-result-compact-20261026.json'
    out.write_text(json.dumps(compact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    entry = {
        'schema': compact['format'],
        'summary': str(out),
        'publication': str(BASE/'h716r-result-closure-publication-20261026.json'),
        'remote_episode_id': REMOTE_EPISODE,
        'hypothesis_id': compact['hypothesis_id'],
        'decision': compact['decision'],
        'root_cause_classification': compact['root_cause_classification'],
        'result_now_authenticated_and_read': True,
        'selection_prefix_passed': compact['selection_prefix_passed'],
        'holdout_prefix_passed': compact['holdout_prefix_passed'],
        'new_strategy_evaluations': 1,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    action = 'Retain H600 as the best paper challenger and H534 all-hours as paper reference; close H716R after its causal repair readback, do not rerun its edge-cap path, and require a genuinely distinct hypothesis or owner-scoped execution bundle before another admission.'
    for path in FILES:
        state = json.loads(path.read_text(encoding='utf-8'))
        state['latest_h716r_cloud_validation'] = entry
        state['active_run'] = None
        state['current_inflight_run'] = None
        state['current_inflight_runs'] = []
        state['current_cloud_run_status'] = 'completed_success_authenticated_readback_rejected'
        state['status'] = 'h716r_terminal_rejected'
        state['state'] = 'causal_cash_ledger_contract_repair_rejected_strategy_unstable'
        state['next_action'] = action
        state['actual_fills'] = 0
        state['verified_canary_fills'] = 0
        state['live_orders_enabled'] = False
        state['buying_paused'] = True
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    receipt = {
        'format': 'h716r-terminal-reconciliation-v1',
        'run_id': RUN_ID,
        'remote_episode_id': REMOTE_EPISODE,
        'result_now_authenticated_and_read': True,
        'decision': compact['decision'],
        'updated_files': [str(p) for p in FILES],
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    (STUDY/'h716r-terminal-reconciliation-20261026.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
