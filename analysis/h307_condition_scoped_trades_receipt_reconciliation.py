"""Use condition-scoped Data API v2 trades as a fallback source.

H306 found the documented ``/v2/trades?user=`` shape empty for the existing
owners even though ``/v2/activity?user=`` contained BTC rows.  H307 obtains
BTC condition IDs from v2 positions, queries the documented condition-scoped
trades shape in groups of at most twenty, filters ``proxy_wallet`` to the
sealed owners, and fetches only the returned transaction receipts.
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
        _lower_address,
        _tx,
    )
    from .h301_owner_scoped_settlement_history_scan import (
        _activity_condition_ids,
        _reconcile,
        _rpc_call,
    )
    from .h302_v2_position_manager_settlement_scan import _v2_receipt_logs
    from .h303_data_api_v2_status_reconciliation import (
        BASE_URL,
        MAX_ROWS_PER_FEED,
        _api_get,
        _condition,
        _first,
        _is_btc_row,
        _pages,
        _text,
    )
    from .h306_data_api_v2_trades_receipt_reconciliation import _trade_as_activity
except ImportError:  # direct ``python analysis/h307_*.py`` execution
    from h300_owner_scoped_payout_transfer_capture import decode_receipt_logs, is_btc_five_minute, _lower_address, _tx
    from h301_owner_scoped_settlement_history_scan import _activity_condition_ids, _reconcile, _rpc_call
    from h302_v2_position_manager_settlement_scan import _v2_receipt_logs
    from h303_data_api_v2_status_reconciliation import BASE_URL, MAX_ROWS_PER_FEED, _api_get, _condition, _first, _is_btc_row, _pages, _text
    from h306_data_api_v2_trades_receipt_reconciliation import _trade_as_activity


MAX_ACCOUNTS = 4
MAX_CONDITIONS_PER_REQUEST = 20
MAX_RECEIPTS = 200


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _position_conditions(
    owner: str,
    *,
    fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    base_url: str,
) -> tuple[list[str], dict[str, int], dict[str, str | None], int]:
    common = {"user": owner, "limit": MAX_ROWS_PER_FEED, "sort_by": "TIMESTAMP", "sort_direction": "DESC"}
    status_counts: dict[str, int] = {}
    failures: dict[str, str | None] = {}
    all_conditions: set[str] = set()
    pages_total = 0
    for status in ("REDEEMABLE", "CLOSED"):
        rows, _truncated, pages, failure = _pages(
            base_url, "/v2/positions", {**common, "status": status}, fetcher=fetcher
        )
        pages_total += pages
        failures[status.lower()] = failure
        btc = [row for row in rows if _is_btc_row(row)]
        status_counts[status.lower()] = len(btc)
        for row in btc:
            condition = _condition(_first(row, ("condition_id", "conditionId", "condition", "market")))
            if condition:
                all_conditions.add(condition)
    return sorted(all_conditions), status_counts, failures, pages_total


def _receipt_join(owner: str, rows: list[dict[str, Any]], rpc_call: Callable[[str, list[Any]], Any]) -> dict[str, Any]:
    btc_rows = [normalized for raw in rows if (normalized := _trade_as_activity(raw)) and is_btc_five_minute(normalized)]
    condition_ids = _activity_condition_ids(btc_rows)
    txs = list(dict.fromkeys(row["transaction_ref"] for row in btc_rows))[:MAX_RECEIPTS]
    transfer_rows: list[dict[str, Any]] = []
    payout_rows: list[dict[str, Any]] = []
    confirmed = 0
    failures = 0
    legacy_payouts = 0
    v2_payouts = 0
    v2_unbound = 0
    for tx_hash in txs:
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict):
                raise ValueError("receipt_missing")
            old_transfers, old_payouts, old_confirmed = decode_receipt_logs(receipt, owner=owner)
            new_transfers, new_payouts, admitted, unbound, new_confirmed = _v2_receipt_logs(
                receipt, owner=owner, condition_ids=condition_ids
            )
            confirmed += int(old_confirmed or new_confirmed)
            transfer_rows.extend(old_transfers + new_transfers)
            payout_rows.extend(old_payouts + new_payouts)
            legacy_payouts += len(old_payouts)
            v2_payouts += admitted
            v2_unbound += unbound
        except Exception:
            failures += 1
    reconciliation = _reconcile(btc_rows, transfer_rows, payout_rows, owner)
    cash_release = sum(
        value.get("cash_release_transaction_rows", 0)
        for value in reconciliation.values()
        if isinstance(value, dict)
    )
    return {
        "normalized_btc_trade_rows": len(btc_rows),
        "distinct_btc_trade_transactions": len(txs),
        "condition_ids_in_trades": len(condition_ids),
        "confirmed_trade_receipts": confirmed,
        "receipt_fetch_failures": failures,
        "legacy_settlement_receipt_rows": legacy_payouts,
        "v2_settlement_receipt_rows": v2_payouts,
        "v2_unbound_payout_rows": v2_unbound,
        "cash_release_receipt_rows": cash_release,
        "cash_reconciliation_by_collateral_token": reconciliation,
    }


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
    conditions, status_counts, status_failures, position_pages = _position_conditions(
        owner, fetcher=data_fetcher, base_url=base_url
    )
    raw_rows: list[dict[str, Any]] = []
    condition_chunks = 0
    trade_pages = 0
    trade_failures: list[str] = []
    owner_matches = 0
    btc_matches = 0
    seen: set[tuple[str, str]] = set()
    for chunk in _chunks(conditions, MAX_CONDITIONS_PER_REQUEST):
        condition_chunks += 1
        payload, truncated, pages, failure = _pages(
            base_url,
            "/v2/trades",
            {"condition": ",".join(chunk), "limit": MAX_ROWS_PER_FEED, "taker_only": False,
             "filter_type": "TOKENS", "filter_amount": 0},
            fetcher=data_fetcher,
        )
        trade_pages += pages
        if failure:
            trade_failures.append(failure)
        for row in payload:
            if not isinstance(row, dict):
                continue
            proxy = _lower_address(_first(row, ("proxy_wallet", "proxyWallet", "wallet_address", "walletAddress")))
            if proxy != owner:
                continue
            owner_matches += 1
            condition = _condition(_first(row, ("condition_id", "conditionId", "condition")))
            if condition not in chunk or not is_btc_five_minute(row):
                continue
            btc_matches += 1
            key = (_tx(_first(row, ("transaction_hash", "transactionHash", "tx_hash", "txHash"))), condition)
            if key[0] and key not in seen:
                seen.add(key)
                raw_rows.append(row)
    joined = _receipt_join(owner, raw_rows, rpc_call)
    return {
        "owner_address_sha256": hashlib.sha256(owner.encode("ascii")).hexdigest(),
        "position_pages": position_pages,
        "redeemable_btc_rows": status_counts.get("redeemable", 0),
        "closed_btc_rows": status_counts.get("closed", 0),
        "status_failures": status_failures,
        "condition_ids": len(conditions),
        "condition_chunks": condition_chunks,
        "trade_pages": trade_pages,
        "trade_failures": trade_failures,
        "owner_trade_rows": owner_matches,
        "btc_trade_rows": btc_matches,
        **joined,
        "private_condition_ids": conditions,
        "private_btc_trade_rows": raw_rows,
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
        "format": "h307-condition-scoped-trades-receipt-reconciliation-v1",
        "accounts": reports,
        "scope": "read-only condition-scoped v2 trades to owner-filtered receipt join",
        "redeemable_btc_rows": sum(r["redeemable_btc_rows"] for r in reports),
        "closed_btc_rows": sum(r["closed_btc_rows"] for r in reports),
        "condition_ids": sum(r["condition_ids"] for r in reports),
        "condition_chunks": sum(r["condition_chunks"] for r in reports),
        "owner_trade_rows": sum(r["owner_trade_rows"] for r in reports),
        "btc_trade_rows": sum(r["btc_trade_rows"] for r in reports),
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
    print(json.dumps({k: result[k] for k in (
        "format", "redeemable_btc_rows", "closed_btc_rows", "condition_ids", "condition_chunks",
        "owner_trade_rows", "btc_trade_rows", "normalized_btc_trade_rows", "confirmed_trade_receipts",
        "legacy_settlement_receipt_rows", "v2_settlement_receipt_rows", "cash_release_receipt_rows")}, sort_keys=True))


if __name__ == "__main__":
    main()

