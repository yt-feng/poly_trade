#!/usr/bin/env python3
"""Seal and publish the bounded cancellation/repair record for the slow SVM run."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
OUT = BASE / 'quantml-svm-runtime-cancel-20261026.json'
DELIVERY = BASE / '.quantml-svm-runtime-cancel-delivery'
RID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
RUN_ID = '40269023541'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def main() -> None:
    if OUT.exists() or DELIVERY.exists():
        raise SystemExit('already_exists')
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    report = {
        'format': 'quantml-svm-runtime-cancel-v1',
        'created_utc': now,
        'workflow_run_id': RUN_ID,
        'workflow_status': 'cancelled_by_owner_after_bounded_efficiency_review',
        'observed_duration_seconds': 853,
        'observed_step': 'Run frozen calibrated SVM challenger',
        'root_cause': 'Exact SVC(probability=True) repeated 79 expanding fits with internal probability calibration; no bounded intermediate result after 14m13s.',
        'repair': 'Replace the nonlinear branch with a fixed Nystroem RBF map (gamma=0.5, n_components=128) and past-only sigmoid calibration on LinearSVC; preserve all cash, fee, slippage and holdout gates.',
        'evidence': {'run_url': 'https://github.com/yt-feng/poly_trade/actions/runs/40269023541', 'commit': '9d9c74229396a2e59615897e2a099e3d03062166', 'terminal_page_status': 'Cancelled'},
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    OUT.chmod(0o600)
    recipient = vault.load_json_bytes(vault.read_bounded(ROOT / 'research_vault/recipient.json', 4096), 4096)
    vault.validate_recipient(recipient, RID)
    DELIVERY.mkdir(mode=0o700)
    payload = {'format': report['format'], 'report': report, 'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False, 'plaintext_published': False}
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical(payload), recipient, RID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({'version': 1, 'status': 'complete', 'stage': 'sealed_delivery', 'strategy_evaluated': False, 'live_orders_enabled': False, 'plaintext_published': False, 'sealed_parts': 1}) + b'\n', 0o644)
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools' / 'publish_quality_first_journal.py'),
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade', '--run-id', RUN_ID,
        '--run-attempt', '1', '--recipient-id', RID, '--cell-id', 'quantml-svm-runtime-cancel-20261026',
    ], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode:
        print(json.dumps({'status': 'sealed_local_publication_pending', 'run_id': RUN_ID, 'stderr': proc.stderr[-400:]}, ensure_ascii=False))
        raise SystemExit(2)
    print(proc.stdout.strip().splitlines()[-1])


if __name__ == '__main__':
    main()
