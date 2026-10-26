#!/usr/bin/env python3
"""Decrypt one verified UTC regime-gate envelope and retain compact metrics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
PART = BASE / 'h534-time-regime-gate-run-40296621939-part-0000.vault'
OUT = BASE / 'h534-time-regime-gate-result-20261026.json'
IDENTITY = Path('/Users/ytfeng/.codex/private/poly-quality-first-20261015')
sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def main() -> None:
    if OUT.exists():
        raise SystemExit('result_already_exists')
    raw_key = IDENTITY / 'audit-private.key'
    if not raw_key.is_file() or raw_key.stat().st_size != 32:
        raise SystemExit('local_identity_key_unavailable')
    private = X25519PrivateKey.from_private_bytes(raw_key.read_bytes())
    try:
        plaintext = vault.unseal(PART.read_bytes(), private)
    finally:
        del private
    document = vault.load_json_bytes(plaintext, 16 * 1024 * 1024)
    if document.get('format') != 'old-clean-h534-time-regime-gate-sealed-v1':
        raise SystemExit('unexpected_result_format')
    summary = document.get('summary')
    if not isinstance(summary, dict):
        raise SystemExit('missing_summary')
    policies = summary.get('policies')
    if not isinstance(policies, dict):
        raise SystemExit('missing_policies')
    result = {
        'schema': 'quantml-h534-time-regime-gate-readback-v1',
        'workflow_run_id': 40296621939,
        'workflow_head_sha': '3c1d1b2ccf94fe9d582e0be1cc907019cf9017a1',
        'source_data_commit': summary.get('source_commit'),
        'remote_envelope_verified': True,
        'result': {
            'hypothesis_id': summary.get('hypothesis_id'),
            'decision': summary.get('decision'),
            'policies': policies,
            'protocol': summary.get('protocol'),
            'selection_rule': summary.get('selection_rule'),
            'root_cause_scope': summary.get('root_cause_scope'),
            'new_data_collected': summary.get('new_data_collected'),
            'model_weights_changed': summary.get('model_weights_changed'),
            'owner_scoped_evidence_required': summary.get('owner_scoped_evidence_required'),
        },
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'decrypted_compact_readback', 'output': str(OUT)}))


if __name__ == '__main__':
    main()
