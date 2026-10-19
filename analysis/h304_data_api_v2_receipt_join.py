"""Join official Data API v2 lifecycle rows to confirmed on-chain receipts.

H303 established that the v2 status surface contains BTC five-minute rows but
did not treat those rows as settlement.  H304 uses the same existing owner
activity plus the v2 activity/status references to narrow the already bounded
V2 receipt scan.  A row enters the cash ledger only through H302's confirmed
receipt, owner-directed collateral transfer and condition binding checks.  No
order, redemption, merge, allowance or balance mutation is performed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from .h302_v2_position_manager_settlement_scan import capture_account as capture_v2
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
except ImportError:  # direct ``python analysis/h304_*.py`` execution
    from h302_v2_position_manager_settlement_scan import capture_account as capture_v2
    from h303_data_api_v2_status_reconciliation import (
        BASE_URL,
        MAX_ROWS_PER_FEED,
        _api_get,
        _condition,
        _first,
        _is_btc_row,
        _pages,
        _text,
    )


MAX_ACCOUNTS = 4


def _tx(row: dict[str, Any]) -> str:
    value = _first(row, ("transaction_hash", "transactionHash", "tx_hash", "txHash", "transaction_ref"))
    text = _text(value).lower()
    return text if len(text) == 66 and text.startswith("0x") else ""


def _activity_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _tx(row),
        _text(_first(row, ("type", "activity_type", "event_type"))).upper(),
        _condition(_first(row, ("condition_id", "conditionId", "condition", "market"))),
    )


def _join_activity(existing: Iterable[dict[str, Any]], api_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    joined: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in list(existing) + list(api_rows):
        if not isinstance(row, dict) or not _is_btc_row(row):
            continue
        key = _activity_key(row)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        joined.append(row)
    return joined


def capture_account(
    account: dict[str, Any],
    *,
    data_fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    rpc_call: Callable[[str, list[Any]], Any],
    base_url: str = BASE_URL,
    max_history_blocks: int = 250_000,
    chunk_blocks: int = 5_000,
) -> dict[str, Any]:
    owner = _text(account.get("owner")).lower()
    if not owner:
        raise ValueError("account_owner_required")
    common = {"user": owner, "limit": MAX_ROWS_PER_FEED, "sort_by": "TIMESTAMP", "sort_direction": "DESC"}
    redeemable, redeemable_truncated, redeemable_pages, redeemable_failure = _pages(
        base_url, "/v2/positions", {**common, "status": "REDEEMABLE"}, fetcher=data_fetcher
    )
    closed, closed_truncated, closed_pages, closed_failure = _pages(
        base_url, "/v2/positions", {**common, "status": "CLOSED"}, fetcher=data_fetcher
    )
    activity, activity_truncated, activity_pages, activity_failure = _pages(
        base_url, "/v2/activity", common, fetcher=data_fetcher
    )
    btc_redeemable = [row for row in redeemable if _is_btc_row(row)]
    btc_closed = [row for row in closed if _is_btc_row(row)]
    btc_activity = [row for row in activity if _is_btc_row(row)]
    joined_activity = _join_activity(account.get("existing_activity") or [], btc_activity)
    v2_report = capture_v2(
        {"label": account.get("label", ""), "owner": owner, "activity": joined_activity},
        rpc_call=rpc_call,
        max_history_blocks=max_history_blocks,
        chunk_blocks=chunk_blocks,
    )
    return {
        "label": str(account.get("label") or ""),
        "owner_address_sha256": __import__("hashlib").sha256(owner.encode("ascii")).hexdigest(),
        "redeemable_btc_rows": len(btc_redeemable),
        "closed_btc_rows": len(btc_closed),
        "activity_btc_rows": len(btc_activity),
        "joined_btc_activity_rows": len(joined_activity),
        "redeemable_btc_condition_ids": len({c for row in btc_redeemable for c in [_condition(_first(row, ("condition_id", "conditionId", "condition", "market")))] if c}),
        "closed_btc_condition_ids": len({c for row in btc_closed for c in [_condition(_first(row, ("condition_id", "conditionId", "condition", "market")))] if c}),
        "activity_pages": activity_pages,
        "redeemable_pages": redeemable_pages,
        "closed_pages": closed_pages,
        "activity_truncated": activity_truncated,
        "redeemable_truncated": redeemable_truncated,
        "closed_truncated": closed_truncated,
        "activity_failure": activity_failure,
        "redeemable_failure": redeemable_failure,
        "closed_failure": closed_failure,
        "v2_receipt_join": v2_report,
        # These raw rows are only carried inside the encrypted cloud payload;
        # the CLI prints counts and never prints them.
        "private_status_rows": {"redeemable": btc_redeemable, "closed": btc_closed},
        "private_activity_rows": btc_activity,
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
    max_history_blocks: int = 250_000,
    chunk_blocks: int = 5_000,
) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    reports = [capture_account(row, data_fetcher=data_fetcher, rpc_call=rpc_call, base_url=base_url,
                               max_history_blocks=max_history_blocks, chunk_blocks=chunk_blocks)
               for row in rows]
    return {
        "format": "h304-data-api-v2-receipt-join-v1",
        "accounts": reports,
        "scope": "read-only v2 status/activity to confirmed owner-scoped receipt join",
        "redeemable_btc_rows": sum(r["redeemable_btc_rows"] for r in reports),
        "closed_btc_rows": sum(r["closed_btc_rows"] for r in reports),
        "activity_btc_rows": sum(r["activity_btc_rows"] for r in reports),
        "joined_btc_activity_rows": sum(r["joined_btc_activity_rows"] for r in reports),
        "settlement_receipt_rows": sum(r["v2_receipt_join"].get("settlement_receipt_rows", 0) for r in reports),
        "cash_release_receipt_rows": sum(r["v2_receipt_join"].get("cash_release_receipt_rows", 0) for r in reports),
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
    parser.add_argument("--max-history-blocks", type=int, default=250_000)
    parser.add_argument("--chunk-blocks", type=int, default=5_000)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    from h301_owner_scoped_settlement_history_scan import _rpc_call
    result = capture(document.get("accounts") or [], data_fetcher=_api_get,
                     rpc_call=lambda method, params: _rpc_call(args.rpc_url, method, params),
                     base_url=args.base_url, max_history_blocks=args.max_history_blocks,
                     chunk_blocks=args.chunk_blocks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "computed", "format": result["format"],
                      "redeemable_btc_rows": result["redeemable_btc_rows"],
                      "closed_btc_rows": result["closed_btc_rows"],
                      "activity_btc_rows": result["activity_btc_rows"],
                      "settlement_receipt_rows": result["settlement_receipt_rows"],
                      "cash_release_receipt_rows": result["cash_release_receipt_rows"]}, sort_keys=True))


if __name__ == "__main__":
    main()

