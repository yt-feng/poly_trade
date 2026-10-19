"""Read-only reconciliation of Polymarket Data API v2 status/activity.

This is a source-coverage audit, not a settlement receipt and not a Canary
fill.  It asks the official v2 API for REDEEMABLE and CLOSED positions plus
the user's activity feed, filters the BTC five-minute subset, and reports only
bounded counts, condition/transaction overlaps and status coverage.  Raw rows
stay inside the encrypted workflow payload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .h300_owner_scoped_payout_transfer_capture import (
        _lower_address,
        _tx,
        is_btc_five_minute,
        normalize_activity_row,
    )
except ImportError:  # direct ``python analysis/h303_*.py`` execution
    from h300_owner_scoped_payout_transfer_capture import (
        _lower_address,
        _tx,
        is_btc_five_minute,
        normalize_activity_row,
    )


MAX_ACCOUNTS = 4
MAX_ROWS_PER_FEED = 1_000
MAX_PAGES_PER_FEED = 8
MAX_RETRIES = 3
BASE_URL = "https://data-api.polymarket.com"
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _first(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _condition(value: Any) -> str:
    text = _text(value).lower()
    return text if re.fullmatch(r"0x[0-9a-f]{64}", text) else ""


def _slug(row: dict[str, Any]) -> str:
    return _text(_first(row, ("slug", "event_slug", "eventSlug", "market_slug", "marketSlug"))).lower()


def _is_btc_row(row: dict[str, Any]) -> bool:
    normalized = normalize_activity_row(row)
    if normalized is not None and is_btc_five_minute(normalized):
        return True
    return bool(re.search(r"(?:^|[-_])(btc|bitcoin)[-_](?:updown|up_down)[-_]5m(?:[-_]|$)", _slug(row)))


def _api_get(base_url: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    query = {key: value for key, value in params.items() if value not in (None, "")}
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urlencode(query)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "poly-trade-h303/1.0"})
    last_status = None
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("api_response_must_be_object")
                return payload
        except Exception as exc:
            last_status = type(exc).__name__
            if attempt + 1 == MAX_RETRIES:
                raise RuntimeError("data_api_v2_request_failed") from exc
            time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(last_status or "data_api_v2_request_failed")


def _pages(
    base_url: str,
    path: str,
    params: dict[str, Any],
    *,
    fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, int, str | None]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    truncated = False
    failure: str | None = None
    while pages < MAX_PAGES_PER_FEED and len(rows) < MAX_ROWS_PER_FEED:
        request_params = dict(params)
        if cursor:
            request_params["cursor"] = cursor
        try:
            payload = fetcher(base_url, path, request_params)
        except Exception as exc:
            failure = type(exc).__name__
            break
        pages += 1
        data = payload.get("data")
        if isinstance(data, list):
            rows.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            rows.append(data)
        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        has_more = bool(pagination.get("has_more")) if isinstance(pagination, dict) else False
        if not has_more or not next_cursor:
            break
        cursor = str(next_cursor)
    if len(rows) >= MAX_ROWS_PER_FEED or pages >= MAX_PAGES_PER_FEED:
        truncated = bool(cursor)
    return rows[:MAX_ROWS_PER_FEED], truncated, pages, failure


def _feed_summary(rows: list[dict[str, Any]], *, existing_tx: set[str]) -> dict[str, Any]:
    btc = [row for row in rows if _is_btc_row(row)]
    conditions = {_condition(_first(row, ("condition_id", "conditionId", "condition"))) for row in btc}
    conditions.discard("")
    txs = {_tx(_first(row, ("transaction_hash", "transactionHash", "tx_hash", "txHash"))) for row in btc}
    txs.discard("")
    kinds: dict[str, int] = {}
    for row in btc:
        kind = _text(_first(row, ("type", "activity_type", "event_type"))).upper() or "UNKNOWN"
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "rows": len(rows),
        "btc_five_minute_rows": len(btc),
        "btc_condition_ids": len(conditions),
        "btc_transaction_refs": len(txs),
        "btc_existing_transaction_overlap": len(txs & existing_tx),
        "btc_activity_types": dict(sorted(kinds.items())),
    }


def capture_account(
    account: dict[str, Any],
    *,
    fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    base_url: str = BASE_URL,
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    existing_tx = {
        tx for raw in (account.get("existing_activity") or [])
        if isinstance(raw, dict)
        for tx in [_tx(_first(raw, ("transaction_hash", "transactionHash", "tx_hash", "txHash")))]
        if tx
    }
    common = {"user": owner, "limit": MAX_ROWS_PER_FEED, "sort_by": "TIMESTAMP", "sort_direction": "DESC"}
    redeemable, redeemable_truncated, redeemable_pages, redeemable_failure = _pages(
        base_url, "/v2/positions", {**common, "status": "REDEEMABLE"}, fetcher=fetcher
    )
    closed, closed_truncated, closed_pages, closed_failure = _pages(
        base_url, "/v2/positions", {**common, "status": "CLOSED"}, fetcher=fetcher
    )
    activity, activity_truncated, activity_pages, activity_failure = _pages(
        base_url, "/v2/activity", common, fetcher=fetcher
    )
    btc_redeemable = [row for row in redeemable if _is_btc_row(row)]
    btc_closed = [row for row in closed if _is_btc_row(row)]
    redeemable_conditions = {
        condition for row in btc_redeemable
        for condition in [_condition(_first(row, ("condition_id", "conditionId", "condition")))]
        if condition
    }
    closed_conditions = {
        condition for row in btc_closed
        for condition in [_condition(_first(row, ("condition_id", "conditionId", "condition")))]
        if condition
    }
    activity_summary = _feed_summary(activity, existing_tx=existing_tx)
    activity_btc = [row for row in activity if _is_btc_row(row)]
    activity_redeem = [row for row in activity_btc if _text(_first(row, ("type", "activity_type", "event_type"))).upper() in {"REDEEM", "REDEEMED", "PAYOUT_REDEMPTION"}]
    activity_merge = [row for row in activity_btc if _text(_first(row, ("type", "activity_type", "event_type"))).upper() in {"MERGE", "MERGED"}]
    return {
        "owner_address_sha256": _digest(owner),
        "redeemable_rows": len(redeemable),
        "redeemable_btc_rows": len(btc_redeemable),
        "redeemable_btc_condition_ids": len(redeemable_conditions),
        "redeemable_btc_cash_value_sum": sum(float(row.get("current_value") or 0) for row in btc_redeemable),
        "closed_rows": len(closed),
        "closed_btc_rows": len(btc_closed),
        "closed_btc_condition_ids": len(closed_conditions),
        "closed_btc_realized_pnl_sum": sum(float(row.get("realized_pnl") or 0) for row in btc_closed),
        "activity": activity_summary,
        "activity_btc_redeem_rows": len(activity_redeem),
        "activity_btc_merge_rows": len(activity_merge),
        "redeemable_pages": redeemable_pages,
        "closed_pages": closed_pages,
        "activity_pages": activity_pages,
        "redeemable_truncated": redeemable_truncated,
        "closed_truncated": closed_truncated,
        "activity_truncated": activity_truncated,
        "redeemable_failure": redeemable_failure,
        "closed_failure": closed_failure,
        "activity_failure": activity_failure,
        "cash_receipt_rows": 0,
        "balance_continuity_proven": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def capture(
    accounts: Iterable[dict[str, Any]],
    *,
    fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]] = _api_get,
    base_url: str = BASE_URL,
) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    reports = [capture_account(row, fetcher=fetcher, base_url=base_url) for row in rows]
    return {
        "format": "h303-data-api-v2-status-reconciliation-v1",
        "accounts": reports,
        "scope": "read-only official Data API v2 status/activity source coverage",
        "redeemable_btc_rows": sum(row["redeemable_btc_rows"] for row in reports),
        "closed_btc_rows": sum(row["closed_btc_rows"] for row in reports),
        "activity_btc_redeem_rows": sum(row["activity_btc_redeem_rows"] for row in reports),
        "activity_btc_merge_rows": sum(row["activity_btc_merge_rows"] for row in reports),
        "cash_receipt_rows": 0,
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
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = capture(document.get("accounts") or [], base_url=args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "computed",
        "format": result["format"],
        "redeemable_btc_rows": result["redeemable_btc_rows"],
        "closed_btc_rows": result["closed_btc_rows"],
        "activity_btc_redeem_rows": result["activity_btc_redeem_rows"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
