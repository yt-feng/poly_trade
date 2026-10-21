"""Fail-closed offline validator for official Polymarket V2 settlement paths.

The validator consumes a captured, normalized artifact only.  It never fetches
receipts, reads a wallet, signs or submits an order, and never counts a
synthetic path as a Canary.  It makes the V2 path and cash-release evidence
explicit before the existing one-ledger maker validator can be used.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


SCHEMA = "authenticated-maker-pair-v2-path-bundle-v1"
REQUIRED_EVENTS = 10
EXCHANGE_CONTRACTS = {
    "0xe111180000d2663c0091e4f400237545b87b996b": "ctf-exchange-v2",
    "0xe2222d279d744050d28e00520010520000310f59": "neg-risk-ctf-exchange-v2",
}
MATCH_PATHS = {"COMPLEMENTARY", "MINT", "MERGE"}


def _text(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field}_required")
    return str(value).strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return result


def _int(value: Any, field: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_integer") from exc


def _failure(reason: str, *, events: int = 0) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "required_events": REQUIRED_EVENTS,
        "events": events,
        "offline_consistency_passed": False,
        "receipt_authenticity_verified": False,
        "gate_passed": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "failure_reason": reason,
    }


def validate_path_event(row: Mapping[str, Any], *, owner: str, ledger_id: str, chain_id: str) -> dict[str, Any] | None:
    """Validate one path row; return a redacted normalized result or None."""
    if not isinstance(row, Mapping):
        return _failure("path_event_must_be_object")
    try:
        if _text(row.get("owner"), "owner") != owner:
            return _failure("owner_scope_mismatch")
        if _text(row.get("ledger_id"), "ledger_id") != ledger_id:
            return _failure("ledger_id_mismatch")
        if _text(row.get("chain_id"), "chain_id") != chain_id:
            return _failure("chain_id_mismatch")
        contract = _text(row.get("exchange_contract"), "exchange_contract")
        version = _text(row.get("exchange_version"), "exchange_version").lower()
        path = _text(row.get("match_path"), "match_path").upper()
        sequence = _int(row.get("sequence_no"), "sequence_no")
        entry_ts = _int(row.get("entry_ts"), "entry_ts")
        filled_ts = _int(row.get("maker_order_filled_ts"), "maker_order_filled_ts")
        payout_ts = _int(row.get("payout_ts"), "payout_ts")
        cash_ts = _int(row.get("cash_available_ts"), "cash_available_ts")
        before = _decimal(row.get("cash_token_balance_before"), "cash_token_balance_before")
        after = _decimal(row.get("cash_token_balance_after"), "cash_token_balance_after")
    except ValueError as exc:
        return _failure(str(exc))
    if contract.lower() not in EXCHANGE_CONTRACTS:
        return _failure("unsupported_exchange_contract")
    if EXCHANGE_CONTRACTS[contract.lower()] != version:
        return _failure("exchange_version_contract_mismatch")
    if path not in MATCH_PATHS:
        return _failure("unsupported_match_path")
    if sequence < 1 or sequence > REQUIRED_EVENTS:
        return _failure("sequence_no_out_of_range")
    if filled_ts > entry_ts:
        return _failure("maker_order_filled_after_entry")
    if payout_ts < filled_ts:
        return _failure("payout_before_maker_fill")
    if cash_ts < payout_ts:
        return _failure("cash_available_before_payout")
    if before < 0 or after < 0:
        return _failure("cash_token_balance_negative")
    filled_refs = row.get("maker_order_filled_refs")
    transfer_refs = row.get("asset_transfer_refs")
    if not isinstance(filled_refs, list) or len(filled_refs) < 2:
        return _failure("maker_order_filled_refs_incomplete")
    if not isinstance(transfer_refs, list) or len(transfer_refs) < 2:
        return _failure("asset_transfer_refs_incomplete")
    if any(not str(value).strip() for value in [*filled_refs, *transfer_refs]):
        return _failure("settlement_receipt_ref_empty")
    try:
        _text(row.get("payout_receipt_id"), "payout_receipt_id")
        _text(row.get("cash_release_receipt_id"), "cash_release_receipt_id")
        _text(row.get("cash_token_balance_checkpoint_id"), "cash_token_balance_checkpoint_id")
    except ValueError as exc:
        return _failure(str(exc))
    ctf_ref = row.get("ctf_operation_ref")
    if path in {"MINT", "MERGE"}:
        try:
            _text(ctf_ref, "ctf_operation_ref")
        except ValueError as exc:
            return _failure(str(exc))
    elif ctf_ref is not None and not str(ctf_ref).strip():
        return _failure("ctf_operation_ref_empty")
    return {
        "sequence_no": sequence,
        "exchange_version": version,
        "exchange_contract": contract,
        "match_path": path,
        "maker_order_filled_count": len(filled_refs),
        "asset_transfer_count": len(transfer_refs),
        "ctf_operation_present": bool(ctf_ref),
        "entry_ts": entry_ts,
        "maker_order_filled_ts": filled_ts,
        "payout_ts": payout_ts,
        "cash_available_ts": cash_ts,
        "cash_token_balance_before": str(before),
        "cash_token_balance_after": str(after),
    }


def validate_v2_path_bundle(document: Any) -> dict[str, Any]:
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        return _failure("invalid_bundle_schema")
    try:
        owner = _text(document.get("owner"), "owner")
        ledger_id = _text(document.get("ledger_id"), "ledger_id")
        chain_id = _text(document.get("chain_id"), "chain_id")
        events = document.get("events")
    except ValueError as exc:
        return _failure(str(exc))
    if not isinstance(events, list) or len(events) != REQUIRED_EVENTS:
        return _failure("exactly_ten_path_events_required", events=len(events) if isinstance(events, list) else 0)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(events, start=1):
        result = validate_path_event(row, owner=owner, ledger_id=ledger_id, chain_id=chain_id)
        if result is None or "failure_reason" in result:
            return result or _failure("path_event_invalid", events=index - 1)
        if result["sequence_no"] != index:
            return _failure("sequence_no_must_be_exactly_one_to_ten", events=index - 1)
        key = f"{result['exchange_contract']}|{result['match_path']}|{result['sequence_no']}"
        if key in seen:
            return _failure("duplicate_path_identity", events=index - 1)
        seen.add(key)
        normalized.append(result)
    return {
        "schema": SCHEMA,
        "required_events": REQUIRED_EVENTS,
        "events": REQUIRED_EVENTS,
        "offline_consistency_passed": True,
        "receipt_authenticity_verified": False,
        "gate_passed": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "exchange_versions": sorted({row["exchange_version"] for row in normalized}),
        "match_paths": sorted({row["match_path"] for row in normalized}),
        "normalized_path_rows": normalized,
        "decision": "path_contract_ready_but_owner_receipt_authentication_and_cash_reconciliation_pending",
    }


__all__ = ["EXCHANGE_CONTRACTS", "MATCH_PATHS", "validate_path_event", "validate_v2_path_bundle"]
