"""Offline CLI for a captured authenticated compound-receipt bundle.

The bundle is an input artifact, not a connector.  This command never opens a
socket, reads a wallet, signs, submits, cancels, sells, redeems, or fetches a
balance.  It prints only a compact validation summary so raw account payloads
are not copied into terminal logs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from authenticated_compound_reconciler import reconcile_authenticated_chain


SCHEMA = "authenticated-compound-bundle-v2"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sanitized_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep account evidence and raw ledger rows out of CLI output."""
    allowed = (
        "gate_passed",
        "fills",
        "required_fills",
        "ending_cash",
        "net_pnl",
        "minimum_cash",
        "minimum_cash_before_entry",
        "failure_reason",
        "owner_scoped",
        "identity_complete",
        "unique_trade_ids",
        "unique_transaction_refs",
        "unique_settlement_receipts",
        "unique_cash_release_receipts",
        "unique_balance_checkpoints",
    )
    return {key: result[key] for key in allowed if key in result}


def validate_bundle_document(document: Any, *, required_fills: int = 10) -> dict[str, Any]:
    """Validate one v2 JSON bundle and return a sanitized compound result.

    The bundle is a captured evidence artifact.  This function deliberately
    has no account, wallet, network, signing, or order capability.  v1 is
    rejected because it does not define the identity and cash anchors needed
    to prove one evolving ledger rather than concatenated/reset rows.
    """
    if not isinstance(document, dict):
        return {"gate_passed": False, "failure_reason": "invalid_bundle_schema"}
    if document.get("schema") != SCHEMA:
        if document.get("schema") == "authenticated-compound-bundle-v1":
            return {"gate_passed": False, "failure_reason": "legacy_schema_not_admissible_use_v2"}
        return {"gate_passed": False, "failure_reason": "invalid_bundle_schema"}
    if type(required_fills) is not int or required_fills != 10:
        return {"gate_passed": False, "failure_reason": "required_fills_fixed_at_ten"}

    owner = _text(document.get("owner"))
    ledger_id = _text(document.get("ledger_id"))
    chain_id = _text(document.get("chain_id"))
    opening = _text(document.get("starting_checkpoint_id"))
    closing = _text(document.get("closing_balance_checkpoint_id"))
    events = document.get("events")
    if not owner:
        return {"gate_passed": False, "failure_reason": "owner_required"}
    if not ledger_id or not chain_id:
        return {"gate_passed": False, "failure_reason": "ledger_identity_required"}
    if not opening or not closing:
        return {"gate_passed": False, "failure_reason": "ledger_anchors_required"}
    if opening == closing:
        return {"gate_passed": False, "failure_reason": "ledger_anchors_must_differ"}
    if not isinstance(events, list):
        return {"gate_passed": False, "failure_reason": "events_list_required"}

    try:
        result = reconcile_authenticated_chain(
            events,
            owner=owner,
            initial_cash=document.get("initial_cash", "10"),
            minimum_order=document.get("minimum_order", "5"),
            required_fills=required_fills,
            starting_checkpoint_id=opening,
            closing_balance_checkpoint_id=closing,
            closing_balance=document.get("closing_balance"),
            require_fee_schedule=True,
        )
    except (TypeError, ValueError) as exc:
        return {"gate_passed": False, "failure_reason": str(exc) or "bundle_validation_error"}
    return _sanitized_result(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--required-fills", type=int, default=10)
    args = parser.parse_args()
    try:
        document = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = validate_bundle_document(document, required_fills=args.required_fills)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"gate_passed": False, "failure_reason": "bundle_read_error"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
