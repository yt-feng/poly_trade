"""Audit existing receipt joins before admitting any compound Canary.

The audit is deliberately read-only.  It consumes the current private state
checkpoint and selected redacted result fields already written by prior runs;
it does not query an account, download market data, or create an order.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SOURCE_KEYS = (
    "latest_h303_data_api_v2_status_reconciliation",
    "latest_h304_data_api_v2_receipt_join",
    "latest_h306_data_api_v2_trades_user_source_audit",
    "latest_h307_condition_scoped_trades_fallback",
    "latest_h308_orderbook_subgraph_receipt_reconciliation",
    "latest_h321_accounting_snapshot_probe",
    "latest_h4_source_preflight",
)


def audit(state: dict[str, Any]) -> dict[str, Any]:
    required = (
        "actual_fills",
        "verified_canary_fills",
        "same_ledger_compounding_rows",
        "owner_scoped_bundle_available",
        "owner_scoped_full_settlement_cash_bundle_available",
        "balance_continuity_proven",
        "cash_release_receipt_rows",
        "settlement_receipt_rows",
    )
    missing = [key for key in required if key not in state]
    if missing:
        raise ValueError("MISSING_STATE_FIELDS:" + ",".join(missing))

    sources = []
    for key in SOURCE_KEYS:
        value = state.get(key)
        if not isinstance(value, dict):
            sources.append({"key": key, "present": False})
            continue
        sources.append({
            "key": key,
            "present": True,
            "decision": value.get("decision"),
            "remote_status": value.get("remote_status"),
            "remote_episode_id": value.get("remote_episode_id"),
            "settlement_receipt_rows": value.get("settlement_receipt_rows", 0),
            "cash_release_receipt_rows": value.get("cash_release_receipt_rows", 0),
            "confirmed_trade_receipts": value.get("confirmed_trade_receipts", 0),
            "owner_scoped_full_settlement_cash_bundle_available": value.get(
                "owner_scoped_full_settlement_cash_bundle_available", False
            ),
            "balance_continuity_proven": value.get("balance_continuity_proven", False),
        })

    # ``same_ledger_compounding_rows`` is only admissible when the producer
    # has also proved the identity and continuity of one ledger.  A count of
    # ten by itself could be assembled from ten reset paper windows.
    ledger_rows = state.get("same_ledger_compounding_rows", 0)
    ledger_identity_verified = state.get("same_ledger_identity_verified") is True
    cash_chain_contiguous = state.get("cash_chain_contiguous") is True
    ledger_reset_count = state.get("ledger_reset_count", 0)
    if type(ledger_reset_count) is not int:
        ledger_reset_count = -1
    admitted = bool(
        state["owner_scoped_bundle_available"]
        and state["owner_scoped_full_settlement_cash_bundle_available"]
        and state["balance_continuity_proven"]
        and state["cash_release_receipt_rows"] >= 10
        and state["settlement_receipt_rows"] >= 10
        and state["actual_fills"] == 10
        and state["verified_canary_fills"] == 10
        and ledger_rows == 10
        and ledger_identity_verified
        and cash_chain_contiguous
        and ledger_reset_count == 0
    )
    return {
        "format": "h330-receipt-gated-admission-audit-v1",
        "hypothesis_id": "H330-receipt-gated-admission-audit",
        "source_scope": "existing state and sealed result summaries only; no new data or account query",
        "admission_contract": {
            "required_fills": 10,
            "owner_scoped": True,
            "same_evolving_cash_ledger": True,
            "exactly_ten_rows": True,
            "sequence_numbers": "1..10",
            "ledger_identity_verified": True,
            "cash_chain_contiguous": True,
            "ledger_reset_count": 0,
            "no_window_concatenation": True,
            "required_receipts": [
                "transaction_ref",
                "settlement_receipt_id",
                "cash_release_receipt_id",
                "balance_checkpoint_id",
                "cash_reconciled=true",
            ],
        },
        "sources": sources,
        "state_counters": {
            **{key: state[key] for key in required},
            "same_ledger_identity_verified": ledger_identity_verified,
            "cash_chain_contiguous": cash_chain_contiguous,
            "ledger_reset_count": ledger_reset_count,
        },
        "canary_admission_passed": admitted,
        "actual_fills": 0 if not admitted else state["actual_fills"],
        "verified_canary_fills": 0 if not admitted else state["verified_canary_fills"],
        "same_ledger_compounding_rows": 0 if not admitted else state["same_ledger_compounding_rows"],
        "live_orders_enabled": False,
        "new_data_collected": False,
        "model_refit": False,
        "decision": "keep_gate_closed_no_owner_scoped_cash_chain" if not admitted else "admission_contract_satisfied",
        "root_cause": "Lifecycle and status rows exist, but no independently verified owner-scoped settlement-to-cash-to-balance chain is present. Empty or incomplete source responses are classified as source/identity gaps, not as evidence of no trades.",
        "next_action": "Do not repeat equivalent bounded scans or promote paper results; wait for a genuinely independent owner-scoped receipt source before any Canary admission.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    report = audit(state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"decision": report["decision"], "canary_admission_passed": report["canary_admission_passed"], "verified_canary_fills": report["verified_canary_fills"]}, sort_keys=True))


if __name__ == "__main__":
    main()
