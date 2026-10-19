"""Directly join authenticated BTC5m activity to transaction receipts.

H304's historical scan required a seed block range before it inspected the
transaction hashes already present in the authenticated account bundle.  H353
removes that circular prerequisite: it fetches only those bounded hashes and
decodes legacy and V2 settlement/collateral logs in each receipt.  It never
submits, cancels, redeems, merges, transfers, or changes allowances.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import URLError

try:
    from .h300_owner_scoped_payout_transfer_capture import (
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
        _lower_address,
        _tx,
    )
    from .h301_owner_scoped_settlement_history_scan import (
        _activity_condition_ids,
        _reconcile,
        _rpc_call,
    )
    from .h302_v2_position_manager_settlement_scan import _v2_receipt_logs
except ImportError:  # direct ``python analysis/h353_*.py`` execution
    from h300_owner_scoped_payout_transfer_capture import (
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
        _lower_address,
        _tx,
    )
    from h301_owner_scoped_settlement_history_scan import _activity_condition_ids, _reconcile, _rpc_call
    from h302_v2_position_manager_settlement_scan import _v2_receipt_logs


MAX_ACCOUNTS = 4
MAX_TRANSACTIONS = 200


def _receipt_failure_code(exc: Exception) -> str:
    """Return a bounded category without retaining provider/error text."""
    if isinstance(exc, URLError) or type(exc).__name__ in {"TimeoutError", "ConnectionError"}:
        return "transport_error"
    if isinstance(exc, RuntimeError) and str(exc) == "receipt_rpc_error":
        return "rpc_error"
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        return "receipt_shape_or_decode_error"
    return "unknown_receipt_error"


def _direct_receipt_join(
    account: dict[str, Any],
    *,
    receipt_fetcher: Callable[[str], dict[str, Any]],
    max_transactions: int = MAX_TRANSACTIONS,
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    normalized = [
        item
        for raw in (account.get("activity") or [])
        if (item := normalize_activity_row(raw))
    ]
    btc_rows = [item for item in normalized if is_btc_five_minute(item)]
    condition_ids = _activity_condition_ids(btc_rows)
    transactions = list(dict.fromkeys(row["transaction_ref"] for row in btc_rows))
    transactions = transactions[:max_transactions]
    transfer_rows: list[dict[str, Any]] = []
    payout_rows: list[dict[str, Any]] = []
    confirmed_receipts = 0
    receipt_failures = 0
    legacy_payout_rows = 0
    v2_payout_rows = 0
    v2_unbound_payout_rows = 0
    receipt_failure_categories: dict[str, int] = {}
    for tx_hash in transactions:
        try:
            receipt = receipt_fetcher(tx_hash)
            if not isinstance(receipt, dict):
                raise ValueError("receipt_missing")
            legacy_transfers, legacy_payouts, legacy_confirmed = decode_receipt_logs(
                receipt, owner=owner
            )
            v2_transfers, v2_payouts, admitted, unbound, v2_confirmed = _v2_receipt_logs(
                receipt, owner=owner, condition_ids=condition_ids
            )
            confirmed_receipts += int(legacy_confirmed or v2_confirmed)
            transfer_rows.extend(legacy_transfers)
            transfer_rows.extend(v2_transfers)
            payout_rows.extend(legacy_payouts)
            payout_rows.extend(v2_payouts)
            legacy_payout_rows += len(legacy_payouts)
            v2_payout_rows += admitted
            v2_unbound_payout_rows += unbound
        except Exception as exc:
            # Keep the failure bounded and redacted; the encrypted payload can
            # retain only aggregate counts and no raw exception text.
            receipt_failures += 1
            code = _receipt_failure_code(exc)
            receipt_failure_categories[code] = receipt_failure_categories.get(code, 0) + 1

    reconciliations = _reconcile(btc_rows, transfer_rows, payout_rows, owner)
    cash_release_rows = sum(
        value.get("cash_release_transaction_rows", 0)
        for value in reconciliations.values()
        if isinstance(value, dict)
    )
    return {
        "label": str(account.get("label") or ""),
        "owner_address_sha256": __import__("hashlib").sha256(owner.encode("ascii")).hexdigest(),
        "btc_five_minute_activity_rows": len(btc_rows),
        "condition_ids": len(condition_ids),
        "transaction_hashes_considered": len(transactions),
        "confirmed_receipts": confirmed_receipts,
        "receipt_fetch_failures": receipt_failures,
        "receipt_failure_categories": receipt_failure_categories,
        "legacy_settlement_receipt_rows": legacy_payout_rows,
        "v2_settlement_receipt_rows": v2_payout_rows,
        "v2_unbound_payout_rows": v2_unbound_payout_rows,
        "cash_release_receipt_rows": cash_release_rows,
        "cash_reconciliation_by_collateral_token": reconciliations,
        "owner_scoped_full_settlement_cash_bundle_available": False,
        "balance_continuity_proven": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def capture(
    accounts: Iterable[dict[str, Any]],
    *,
    receipt_fetcher: Callable[[str], dict[str, Any]],
    max_transactions: int = MAX_TRANSACTIONS,
) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    if not 1 <= max_transactions <= MAX_TRANSACTIONS:
        raise ValueError("max_transactions_out_of_bounds")
    reports = [
        _direct_receipt_join(row, receipt_fetcher=receipt_fetcher, max_transactions=max_transactions)
        for row in rows
    ]
    return {
        "format": "h353-authenticated-trade-receipt-join-v1",
        "accounts": reports,
        "scope": "read-only authenticated BTC5m activity transaction hashes to direct legacy/V2 receipt join",
        "transaction_hashes_considered": sum(r["transaction_hashes_considered"] for r in reports),
        "confirmed_receipts": sum(r["confirmed_receipts"] for r in reports),
        "legacy_settlement_receipt_rows": sum(r["legacy_settlement_receipt_rows"] for r in reports),
        "v2_settlement_receipt_rows": sum(r["v2_settlement_receipt_rows"] for r in reports),
        "cash_release_receipt_rows": sum(r["cash_release_receipt_rows"] for r in reports),
        "receipt_fetch_failures": sum(r["receipt_fetch_failures"] for r in reports),
        "receipt_failure_categories": {
            code: sum(r["receipt_failure_categories"].get(code, 0) for r in reports)
            for code in sorted({code for r in reports for code in r["receipt_failure_categories"]})
        },
        "owner_scoped_full_settlement_cash_bundle_available": False,
        "balance_continuity_proven": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--max-transactions", type=int, default=MAX_TRANSACTIONS)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = capture(
        document.get("accounts") or [],
        receipt_fetcher=lambda tx: _rpc_call(args.rpc_url, "eth_getTransactionReceipt", [tx]),
        max_transactions=args.max_transactions,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("transaction_hashes_considered", "confirmed_receipts", "receipt_fetch_failures", "receipt_failure_categories", "legacy_settlement_receipt_rows", "v2_settlement_receipt_rows", "cash_release_receipt_rows")}, sort_keys=True))


if __name__ == "__main__":
    main()
