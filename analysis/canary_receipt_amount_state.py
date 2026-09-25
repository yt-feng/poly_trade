"""Read-only cash-checkpoint validity; never submits any external action.

The monetary checkpoint is valid only for the exact covered cash-event prefix.
Later cash events, changed economic receipts or an unresolved reconciliation
invalidate it. This validates supplied evidence, not external account truth.
"""
from copy import deepcopy
try:
    from canary_receipt_state import apply_event as structural_apply
except ImportError:
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))
    from canary_receipt_state_legacy import apply_event as structural_apply
from cash_amount_audit import audit_cash_amount, fingerprint

CASH_EVENT_KINDS = {"entry_fill", "sell_fill", "redeem_confirmed", "settlement_finalized"}

def _invalidate(s, reason):
    previous = s.get("cash_amount_audit")
    if previous and previous.get("amount_verified"):
        s.setdefault("cash_amount_audit_history", []).append(deepcopy(previous))
    s["cash_amount_audit"] = {
        "amount_verified": False, "external_truth_verified": False,
        "orders_enabled": False, "reason": reason
    }
    s.update(cash_reconciled=False, cash_reusable=False, completed_roundtrip=False)

def apply_event(original, event):
    s = deepcopy(original)
    hashes = s.setdefault("amount_adapter_event_hashes", {})
    eid = str(event.get("event_id") or "")
    digest = fingerprint(event)
    if not eid:
        raise ValueError("event_id required")
    if eid in hashes:
        if hashes[eid] != digest:
            raise ValueError("CONFLICTING_DUPLICATE_EVENT")
        return s
    if eid in s.get("seen_event_ids", []):
        raise ValueError("LEGACY_DUPLICATE_WITHOUT_VERIFIABLE_PAYLOAD")
    kind = event.get("kind")
    economic = s.setdefault("amount_adapter_economic_hashes", {})
    economic_key = None
    if kind in CASH_EVENT_KINDS:
        identity = str(event.get("trade_id") or event.get("receipt_id") or "")
        if identity:
            economic_key = str(kind) + ":" + identity
            economic_digest = fingerprint({k:v for k,v in event.items() if k != "event_id"})
            if economic_key in economic:
                if economic[economic_key] != economic_digest:
                    raise ValueError("CONFLICTING_ECONOMIC_RECEIPT")
                # Identical economic receipt under a new transport event id.
                hashes[eid] = digest
                return s
    before_cash = set(s.get("cash_affecting_event_ids") or [])
    if kind == "cash_reconciled":
        last_observed = s.get("cash_amount_checkpoint_observed_ms")
        if last_observed is not None and int(event.get("observed_ms", -1)) < last_observed:
            raise ValueError("CASH_CHECKPOINT_TIME_REGRESSION")
        result = audit_cash_amount(
            event.get("opening_cash_checkpoint"), event.get("cash_flow_evidence"),
            event, s.get("cash_affecting_event_ids", []), s.get("expected_cash_asset"))
        if not result["amount_verified"]:
            _invalidate(s, result["reason"])
            s["cash_amount_audit"] = result
            s.update(halt_new_actions=True, halt_reason="cash_amount_audit:"+result["reason"],
                     exit_intent_ready=False, state="cash_amount_unverified")
            hashes[eid] = digest
            return s
        s["cash_amount_audit"] = result
        s = structural_apply(s, event)
        s["cash_amount_checkpoint_observed_ms"] = int(event["observed_ms"])
        s["cash_amount_checkpoint_event_ids"] = sorted(before_cash)
    else:
        s = structural_apply(s, event)
        after_cash = set(s.get("cash_affecting_event_ids") or [])
        if after_cash != before_cash:
            _invalidate(s, "NEW_CASH_EVENT_REQUIRES_NEW_CHECKPOINT")
        covered = set(s.get("cash_checkpoint_covered_event_ids") or [])
        if not after_cash.issubset(covered):
            _invalidate(s, "CASH_CHECKPOINT_DOES_NOT_COVER_CURRENT_PREFIX")
        if kind == "reconciliation_unknown":
            _invalidate(s, "RECONCILIATION_UNKNOWN")
            s["state"] = "unknown_reconciliation"
    valid = bool(s.get("cash_amount_audit", {}).get("amount_verified"))
    covers_prefix = set(s.get("cash_affecting_event_ids") or []).issubset(
        set(s.get("cash_checkpoint_covered_event_ids") or []))
    if not valid or not covers_prefix or s.get("halt_new_actions"):
        s["cash_reusable"] = False
        s["completed_roundtrip"] = False
    if economic_key is not None:
        s.setdefault("amount_adapter_economic_hashes", {})[economic_key] = economic_digest
    s.setdefault("amount_adapter_event_hashes", {})[eid] = digest
    s["amount_audit_scope"] = "read_only_supplied_account_evidence"
    s["orders_submitted_by_contract"] = False
    return s
