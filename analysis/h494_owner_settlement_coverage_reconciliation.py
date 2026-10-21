"""Reconcile the redacted owner-settlement coverage already captured.

This is a readback contract for existing evidence.  It does not fetch a
receipt, access an account, submit a transaction, or collect market data.
The purpose is to preserve the distinction between complete trade-payment
receipts, partial redemption cash links, and a ten-fill compounding chain.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


FORMAT = "h494-owner-settlement-coverage-reconciliation-v1"


def _int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field}_must_be_nonnegative_integer")
    return value


def reconcile(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fail-closed, redacted reconciliation of existing evidence."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot_must_be_object")
    trade_hashes = _int(snapshot.get("authenticated_trade_hashes"), "authenticated_trade_hashes")
    receipts_returned = _int(snapshot.get("receipts_returned"), "receipts_returned")
    receipt_success = _int(snapshot.get("receipt_success"), "receipt_success")
    missing = _int(snapshot.get("missing"), "missing")
    settlement_receipts = _int(snapshot.get("settlement_receipts"), "settlement_receipts")
    redeem_rows = _int(snapshot.get("redeem_rows"), "redeem_rows")
    owner_cash_mints = _int(snapshot.get("owner_cash_mints"), "owner_cash_mints")
    additional_payout_receipts = _int(snapshot.get("additional_payout_receipts"), "additional_payout_receipts")
    partial_cash_links = _int(snapshot.get("partial_cash_links"), "partial_cash_links")
    activity_trades = _int(snapshot.get("activity_trades"), "activity_trades")
    activity_redeems = _int(snapshot.get("activity_redeems"), "activity_redeems")
    subsequent_cash_spends = _int(snapshot.get("subsequent_cash_spends"), "subsequent_cash_spends")

    checks = {
        "all_authenticated_trade_receipts_returned": (
            trade_hashes == receipts_returned == receipt_success and missing == 0
        ),
        "redemption_cash_mints_match_redeem_rows": (
            settlement_receipts == 1 and redeem_rows == owner_cash_mints == 3
        ),
        "no_unseen_payout_receipts_in_trade_batch": additional_payout_receipts == 0,
        "public_activity_complete_for_existing_owner": (
            activity_trades == trade_hashes and activity_redeems == redeem_rows
        ),
        "partial_cash_reuse_is_linked": partial_cash_links == subsequent_cash_spends == 3,
    }
    if not all(checks.values()):
        return {
            "format": FORMAT,
            "checks": checks,
            "all_checks_passed": False,
            "failure_reason": "existing_coverage_fields_are_inconsistent",
            "full_ten_fill_settlement_cash_chain": False,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
        }

    return {
        "format": FORMAT,
        "hypothesis_id": "H494_OWNER_SETTLEMENT_COVERAGE_READBACK",
        "checks": checks,
        "all_checks_passed": True,
        "coverage_complete_for_existing_trade_batch": True,
        "authenticated_trade_rows": trade_hashes,
        "settlement_receipt_rows": settlement_receipts,
        "redeem_rows": redeem_rows,
        "owner_cash_mint_rows": owner_cash_mints,
        "partial_owner_cash_reuse_links": partial_cash_links,
        "full_ten_fill_settlement_cash_chain": False,
        "canary_gate_passed": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "retain_partial_cash_chain_and_require_ten_settlement_cash_balance_rows",
        "root_cause": "Trade-payment receipts are complete for the existing 47-row batch, but one redemption receipt covers only three rows and no additional payout receipts establish ten chronological settlement and reusable-cash links.",
    }


def existing_redacted_snapshot() -> dict[str, int]:
    """Redacted counts from the already sealed H474-H476/H475 readback."""
    return {
        "authenticated_trade_hashes": 47,
        "receipts_returned": 47,
        "receipt_success": 47,
        "missing": 0,
        "settlement_receipts": 1,
        "redeem_rows": 3,
        "owner_cash_mints": 3,
        "additional_payout_receipts": 0,
        "partial_cash_links": 3,
        "activity_trades": 47,
        "activity_redeems": 3,
        "subsequent_cash_spends": 3,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = reconcile(existing_redacted_snapshot())
    if not result["all_checks_passed"]:
        raise SystemExit("h494_coverage_reconciliation_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": FORMAT, "all_checks_passed": True, "canary_gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
