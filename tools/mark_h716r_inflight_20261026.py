#!/usr/bin/env python3
"""Record the single H716R cloud validation as in flight."""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
RUN_ID = '40299229883'
ENTRY = {
    'format': 'h716r-cloud-validation-dispatch-v1',
    'hypothesis_id': 'H716R_H30_SERIAL_EXECUTION_CLOCK_REPAIR',
    'workflow': 'h716r-h30-serial-execution-clock-repair.yml',
    'run_id': RUN_ID,
    'run_attempt': '1',
    'code_commit': '6f551ff3',
    'source_data_commit': 'bbda5f2050685f63153621a54641ebdfab943487',
    'status': 'in_progress',
    'new_data_collected': False,
    'model_refit': False,
    'live_orders_enabled': False,
    'actual_fills': 0,
    'verified_canary_fills': 0,
}
FILES = [BASE/'active-episode.json', BASE/'cycle-progress.json', STUDY/'manifest-summary.json', STUDY/'runtime-state.json', STUDY/'progress.json']


def main() -> None:
    for path in FILES:
        state = json.loads(path.read_text(encoding='utf-8'))
        state['latest_h716r_cloud_validation'] = ENTRY
        state['active_run'] = RUN_ID
        state['current_inflight_run'] = RUN_ID
        state['current_inflight_runs'] = [RUN_ID]
        state['current_cloud_run_mode'] = 'old_clean_protocol_repair'
        state['current_cloud_run_status'] = 'dispatched'
        state['status'] = 'h716r_serial_execution_clock_repair_inflight'
        state['state'] = 'causal_cash_ledger_contract_repair_inflight'
        state['actual_fills'] = 0
        state['verified_canary_fills'] = 0
        state['live_orders_enabled'] = False
        state['buying_paused'] = True
        state['next_action'] = 'Read back H716R cloud terminal state and encrypted delivery; do not count paper output as Canary, and do not dispatch another run.'
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    receipt = {'format': 'h716r-inflight-checkpoint-v1', 'run_id': RUN_ID, 'updated_files': [str(p) for p in FILES], 'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False}
    (STUDY/'h716r-inflight-checkpoint-20261026.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
