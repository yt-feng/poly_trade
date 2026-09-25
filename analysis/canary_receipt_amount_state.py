"""Stricter read-only receipt adapter. Preserves original TP0.98 state semantics.

Legacy structural reconciliation is not sufficient. Supply opening checkpoint,
confirmed signed account flows, and a closing balance checkpoint. Missing or
inconsistent evidence halts new intents; no action is sent by this module.
"""
from copy import deepcopy
try:
    from canary_receipt_state import apply_event as structural_apply
except ImportError:
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'reference'))
    from canary_receipt_state_legacy import apply_event as structural_apply
from cash_amount_audit import audit_cash_amount, fingerprint


def apply_event(original, event):
    s = deepcopy(original)
    hashes = s.setdefault('amount_adapter_event_hashes', {})
    eid = str(event.get('event_id') or '')
    digest = fingerprint(event)
    if not eid:
        raise ValueError('event_id required')
    if eid in hashes:
        if hashes[eid] != digest:
            raise ValueError('CONFLICTING_DUPLICATE_EVENT')
        return s
    if eid in s.get('seen_event_ids', []):
        raise ValueError('LEGACY_DUPLICATE_WITHOUT_VERIFIABLE_PAYLOAD')
    if event.get('kind') == 'cash_reconciled':
        result = audit_cash_amount(event.get('opening_cash_checkpoint'),
             event.get('cash_flow_evidence'), event,
             s.get('cash_affecting_event_ids', []), s.get('expected_cash_asset'))
        s['cash_amount_audit'] = result
        if not result['amount_verified']:
            s.update(cash_reconciled=False, cash_reusable=False, completed_roundtrip=False,
                     halt_new_actions=True, halt_reason='cash_amount_audit:'+result['reason'],
                     exit_intent_ready=False, state='cash_amount_unverified')
            hashes[eid] = digest
            return s
        s = structural_apply(s, event)
    else:
        s = structural_apply(s, event)
    # Even a supplied old state saying "reconciled" may not unlock new money.
    if not s.get('cash_amount_audit', {}).get('amount_verified', False):
        s['cash_reusable'] = False
        s['completed_roundtrip'] = False
    s.setdefault('amount_adapter_event_hashes', {})[eid] = digest
    s['amount_audit_scope'] = 'read_only_supplied_account_evidence'
    s['orders_submitted_by_contract'] = False
    return s
