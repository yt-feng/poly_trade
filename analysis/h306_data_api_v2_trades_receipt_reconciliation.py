"""Reconcile the distinct Data API v2 trades feed to confirmed receipts.

H304 used the account activity/status feeds.  H306 uses the v2 ``trades``
feed with ``taker_only=false`` so maker and taker records are both visible,
then fetches only the referenced transaction receipts.  This is a bounded
source comparison, not a broad event-history scan.  A trade row is never a
Canary fill until the existing owner-scoped settlement, collateral transfer
and reusable-cash gates pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable

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
    from .h302_v2_position_manager_settlement_scan import (
        _v2_receipt_logs,
        V2_COLLATERAL_TOKENS,
    )
    from .h303_data_api_v2_status_reconciliation import (
        BASE_URL,
        MAX_ROWS_PER_FEED,
        _api_get,
        _first,
        _pages,
        _text,
    )
except ImportError:  # direct ``python analysis/h306_*.py`` execution
    from h300_owner_scoped_payout_transfer_capture import (
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
        _lower_address,
        _tx,
    )
    from h301_owner_scoped_settlement_history_scan import _activity_condition_ids, _reconcile, _rpc_call
    from h302_v2_position_manager_settlement_scan import _v2_receipt_logs, V2_COLLATERAL_TOKENS
    from h303_data_api_v2_status_reconciliation import BASE_URL, MAX_ROWS_PER_FEED, _api_get, _first, _pages, _text


MAX_ACCOUNTS = 4
MAX_RECEIPTS = 200


def _trade_as_activity(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    row = dict(raw)
    row["type"] = "TRADE"
    tx = _tx(_first(row, ("transaction_hash", "transactionHash", "tx_hash", "txHash")))
    if tx:
        row["transactionHash"] = tx
    condition = _first(row, ("condition_id", "conditionId", "condition"))
    if condition not in (None, ""):
        row["conditionId"] = condition
    return normalize_activity_row(row)


def capture_account(
    account: dict[str, Any],
    *,
    data_fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    rpc_call: Callable[[str, list[Any]], Any],
    base_url: str = BASE_URL,
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    params = {
        "user": owner,
        "limit": MAX_ROWS_PER_FEED,
        "taker_only": False,
        "filter_type": "TOKENS",
        "filter_amount": 0,
        "start": 1,
    }
    raw_rows, truncated, pages, failure = _pages(
        base_url, "/v2/trades", params, fetcher=data_fetcher
    )
    btc_raw = [row for row in raw_rows if is_btc_five_minute(row)]
    btc_rows = [normalized for raw in btc_raw if (normalized := _trade_as_activity(raw))]
    condition_ids = _activity_condition_ids(btc_rows)
    txs = list(dict.fromkeys(row["transaction_ref"] for row in btc_rows))[:MAX_RECEIPTS]
    transfer_rows: list[dict[str, Any]] = []
    payout_rows: list[dict[str, Any]] = []
    confirmed_receipts = 0
    receipt_failures = 0
    legacy_payout_rows = 0
    v2_payout_rows = 0
    v2_unbound_payout_rows = 0
    for tx_hash in txs:
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict):
                raise ValueError("receipt_missing")
            legacy_transfers, legacy_payouts, legacy_confirmed = decode_receipt_logs(receipt, owner=owner)
            v2_transfers, v2_payouts, admitted, unbound, v2_confirmed = _v2_receipt_logs(
                receipt, owner=owner, condition_ids=condition_ids
            )
            if legacy_confirmed or v2_confirmed:
                confirmed_receipts += 1
            transfer_rows.extend(legacy_transfers)
            transfer_rows.extend(v2_transfers)
            payout_rows.extend(legacy_payouts)
            payout_rows.extend(v2_payouts)
            legacy_payout_rows += len(legacy_payouts)
            v2_payout_rows += admitted
            v2_unbound_payout_rows += unbound
        except Exception:
            receipt_failures += 1
    reconciliations = _reconcile(btc_rows, transfer_rows, payout_rows, owner)
    cash_release_rows = sum(
        value.get("cash_release_transaction_rows", 0)
        for value in reconciliations.values()
        if isinstance(value, dict)
    )
    existing_tx = set()
    for raw in account.get("existing_activity") or []:
        if not isinstance(raw, dict):
            continue
        value = _tx(_first(raw, ("transaction_hash", "transactionHash", "tx_hash", "txHash", "transaction_ref")))
        if value:
            existing_tx.add(value)
    return {
        "owner_address_sha256": hashlib.sha256(owner.encode("ascii")).hexdigest(),
        "raw_btc_trade_rows": len(btc_raw),
        "normalized_btc_trade_rows": len(btc_rows),
        "distinct_btc_trade_transactions": len(txs),
        "existing_activity_transaction_overlap": len(set(txs) & existing_tx),
        "maker_taker_feed": "taker_only=false",
        "pages": pages,
        "truncated": truncated,
        "failure": failure,
        "condition_ids": len(condition_ids),
        "confirmed_trade_receipts": confirmed_receipts,
        "receipt_fetch_failures": receipt_failures,
        "legacy_settlement_receipt_rows": legacy_payout_rows,
        "v2_settlement_receipt_rows": v2_payout_rows,
        "v2_unbound_payout_rows": v2_unbound_payout_rows,
        "cash_release_receipt_rows": cash_release_rows,
        "owner_scoped_full_settlement_cash_bundle_available": False,
        "balance_continuity_proven": False,
        "cash_reconciliation_by_collateral_token": reconciliations,
        # Raw trade rows stay only in the encrypted payload.
        "private_btc_trade_rows": btc_raw,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def capture(
    accounts: Iterable[dict[str, Any]],
    *,
    data_fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    rpc_call: Callable[[str, list[Any]], Any],
    base_url: str = BASE_URL,
) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    reports = [capture_account(row, data_fetcher=data_fetcher, rpc_call=rpc_call, base_url=base_url) for row in rows]
    return {
        "format": "h306-data-api-v2-trades-receipt-reconciliation-v1",
        "accounts": reports,
        "scope": "read-only v2 maker/taker trades feed to direct receipt join",
        "raw_btc_trade_rows": sum(r["raw_btc_trade_rows"] for r in reports),
        "normalized_btc_trade_rows": sum(r["normalized_btc_trade_rows"] for r in reports),
        "confirmed_trade_receipts": sum(r["confirmed_trade_receipts"] for r in reports),
        "legacy_settlement_receipt_rows": sum(r["legacy_settlement_receipt_rows"] for r in reports),
        "v2_settlement_receipt_rows": sum(r["v2_settlement_receipt_rows"] for r in reports),
        "cash_release_receipt_rows": sum(r["cash_release_receipt_rows"] for r in reports),
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
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--rpc-url", required=True)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = capture(document.get("accounts") or [], data_fetcher=_api_get,
                     rpc_call=lambda method, params: _rpc_call(args.rpc_url, method, params),
                     base_url=args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "computed", "format": result["format"],
                      "raw_btc_trade_rows": result["raw_btc_trade_rows"],
                      "normalized_btc_trade_rows": result["normalized_btc_trade_rows"],
                      "confirmed_trade_receipts": result["confirmed_trade_receipts"],
                      "legacy_settlement_receipt_rows": result["legacy_settlement_receipt_rows"],
                      "v2_settlement_receipt_rows": result["v2_settlement_receipt_rows"],
                      "cash_release_receipt_rows": result["cash_release_receipt_rows"]}, sort_keys=True))


if __name__ == "__main__":
    main()

