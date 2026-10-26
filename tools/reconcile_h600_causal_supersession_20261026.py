#!/usr/bin/env python3
"""Supersede the non-causal H600 paper frontier with authenticated H711 evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path

BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
H711 = BASE / 'h711-causal-h600-repair-terminal-reconciliation-20261025.json'
FILES = [BASE/'active-episode.json', BASE/'cycle-progress.json', STUDY/'manifest-summary.json', STUDY/'runtime-state.json', STUDY/'progress.json']
ACTION = ('Do not use the non-causal H600 frontier or H711 repair as a paper candidate and do not retune either on holdout. '
          'Keep live buying paused; choose only a separately registered causal hypothesis or owner-scoped execution bundle, '
          'with the existing QuantML full-corpus boundary unchanged.')


def main() -> None:
    result = json.loads(H711.read_text(encoding='utf-8'))
    required = {
        'status': 'completed_paper_holdout_failed_quarantine',
        'decision': 'quarantine_no_promotion_no_live_action',
        'removed_future_feature': 'btc_move_4m',
        'holdout_passed_rows': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
    }
    for key, value in required.items():
        if result.get(key) != value:
            raise ValueError(f'h711_result_mismatch:{key}')
    entry = {
        'schema': 'h600-causal-supersession-v1',
        'summary': str(H711),
        'hypothesis_id': 'H600_FAVORITE_SIDE_ALIGNMENT_H578_DEPTH',
        'causal_repair_hypothesis_id': 'H711_CAUSAL_H600_FAVORITE_SIDE_DEPTH_REPAIR',
        'remote_run_id': result['run_id'],
        'remote_head_sha': result['head_sha'],
        'source_commit': result['source_commit'],
        'result_now_authenticated_and_read': True,
        'decision': 'supersede_noncausal_h600_frontier',
        'root_cause_classification': 'causal_feature_leakage_removed_holdout_failed',
        'removed_future_feature': result['removed_future_feature'],
        'selection_rows': result['selection_rows'],
        'selection_passed_rows': result['selection_passed_rows'],
        'selection_all_rows_passed': result['selection_all_rows_passed'],
        'holdout_rows': result['holdout_rows'],
        'holdout_passed_rows': result['holdout_passed_rows'],
        'holdout_all_rows_passed': result['holdout_all_rows_passed'],
        'new_strategy_evaluations': 1,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    compact = {
        'format': 'h600-causal-supersession-compact-v1',
        'h600_historical_frontier': 'H600_FAVORITE_SIDE_ALIGNMENT_H578_DEPTH',
        'h711_causal_repair': entry,
        'root_cause': result['root_cause_of_strategy_failure'],
        'decision': entry['decision'],
        'paper_frontier_status': 'no_causal_candidate_qualified',
        'next_action': ACTION,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    compact_path = STUDY / 'h600-causal-supersession-20261026.json'
    compact_path.write_text(json.dumps(compact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for path in FILES:
        state = json.loads(path.read_text(encoding='utf-8'))
        state['latest_h600_causal_supersession'] = {
            **entry,
            'summary': str(compact_path),
            'publication': str(BASE/'h600-causal-supersession-publication-20261026.json'),
        }
        state['h600_paper_candidate_status'] = 'quarantined_noncausal_h711_repair_failed'
        state['h600_strategy_candidate_qualified'] = False
        state['h600_canary_gate_passed'] = False
        state['paper_frontier_challenger'] = 'NONE_CAUSAL_ROUTE_QUALIFIED'
        state['paper_frontier_challenger_status'] = 'h600_noncausal_leakage_removed_holdout_failed; h534_causal_repair_quarantined'
        historical = state.get('latest_paper_frontier_selection')
        if isinstance(historical, dict):
            state['latest_paper_frontier_selection_superseded'] = {
                'historical_record': copy.deepcopy(historical),
                'superseded_by': 'H711_CAUSAL_H600_FAVORITE_SIDE_DEPTH_REPAIR',
                'reason': 'historical H600 paper route used post-decision btc_move_4m; causal holdout failed 0/216',
            }
        state['next_action'] = ACTION
        state['active_run'] = None
        state['current_inflight_run'] = None
        state['current_inflight_runs'] = []
        state['actual_fills'] = 0
        state['verified_canary_fills'] = 0
        state['live_orders_enabled'] = False
        state['buying_paused'] = True
        state['status'] = 'h600_causal_superseded_no_route_qualified'
        state['state'] = 'causal_frontier_superseded_owner_evidence_missing'
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    receipt = {
        'format': 'h600-causal-supersession-state-v1',
        'summary': str(compact_path),
        'remote_run_id': result['run_id'],
        'updated_files': [str(p) for p in FILES],
        'decision': entry['decision'],
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    (STUDY/'h600-causal-supersession-state-20261026.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
