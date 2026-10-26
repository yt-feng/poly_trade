#!/usr/bin/env python3
"""Record successful H716R execution with encrypted-result readback pending."""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
RUN_ID = '40299229883'
EPISODE = '56be280b3f76d67ca15ff37a18a9bbbd104be01c3374564b52326c7e576dc096'
ENTRY = {
    'format': 'h716r-cloud-validation-terminal-v1',
    'hypothesis_id': 'H716R_H30_SERIAL_EXECUTION_CLOCK_REPAIR',
    'workflow': 'h716r-h30-serial-execution-clock-repair.yml',
    'run_id': RUN_ID,
    'run_attempt': '1',
    'remote_episode_id': EPISODE,
    'status': 'completed_success_encrypted_readback_pending',
    'remote_ciphertext_verified': True,
    'result_contents_locally_authenticated': False,
    'local_readback_state': 'ciphertext_verified_pin_unlock_failed',
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
        state['active_run'] = None
        state['current_inflight_run'] = None
        state['current_inflight_runs'] = []
        state['current_cloud_run_status'] = 'completed_success_encrypted_readback_pending'
        state['status'] = 'h716r_terminal_encrypted_readback_pending'
        state['state'] = 'causal_cash_ledger_contract_repaired_readback_pending'
        state['actual_fills'] = 0
        state['verified_canary_fills'] = 0
        state['live_orders_enabled'] = False
        state['buying_paused'] = True
        state['next_action'] = 'Obtain the local private-identity unlock needed to read the H716R sealed result; until then retain the repair as unclassified paper evidence and do not dispatch another run.'
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    receipt = {'format': 'h716r-terminal-readback-pending-checkpoint-v1', 'run_id': RUN_ID, 'remote_episode_id': EPISODE, 'updated_files': [str(p) for p in FILES], 'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False}
    (STUDY/'h716r-terminal-readback-pending-20261026.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
