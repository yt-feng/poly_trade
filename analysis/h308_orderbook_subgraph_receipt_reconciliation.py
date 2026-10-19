"""Join authenticated account trade hashes to Polymarket's orderbook subgraph.

The Data API user/condition feeds can be empty even when the authenticated CLOB
capture contains trades.  The official Goldsky orderbook subgraph stores the
on-chain ``OrderFilledEvent`` directly.  This adapter therefore queries only
transaction hashes already present in the sealed H279 capture, filters the
event maker to the authenticated owner, and reuses the existing receipt and
cash reconciliation gates.  It never treats an indexed trade as a Canary fill
by itself.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

try:
    from .h300_owner_scoped_payout_transfer_capture import (
        _first,
        _lower_address,
        _tx,
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
    )
    from .h301_owner_scoped_settlement_history_scan import _reconcile, _rpc_call
    from .h302_v2_position_manager_settlement_scan import _v2_receipt_logs
except ImportError:  # direct script execution
    from h300_owner_scoped_payout_transfer_capture import (
        _first,
        _lower_address,
        _tx,
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
    )
    from h301_owner_scoped_settlement_history_scan import _reconcile, _rpc_call
    from h302_v2_position_manager_settlement_scan import _v2_receipt_logs


SUBGRAPH_URL = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)
MAX_ACCOUNTS = 4
MAX_TRANSACTION_HASHES = 200
MAX_HASHES_PER_QUERY = 40
MAX_RETRIES = 3
TX = re.compile(r"^0x[0-9a-f]{64}$")

EVENT_QUERY = """
query OrderFilledByTransaction($hashes: [Bytes!]) {
  orderFilledEvents(
    first: 1000
    where: { transactionHash_in: $hashes }
    orderBy: timestamp
    orderDirection: asc
  ) {
    id transactionHash timestamp orderHash maker taker makerAssetId takerAssetId
    makerAmountFilled takerAmountFilled fee
  }
}
"""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(_text(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _event_side(event: dict[str, Any]) -> str:
    # The official subgraph parser treats collateral makerAssetId zero as BUY.
    return "BUY" if _text(event.get("makerAssetId")) in {"0", "0x0"} else "SELL"


def _event_amount(event: dict[str, Any]) -> Decimal | None:
    side = _event_side(event)
    raw = event.get("makerAmountFilled") if side == "BUY" else event.get("takerAmountFilled")
    amount = _decimal(raw)
    return amount / Decimal(10**6) if amount is not None else None


def _source_trade_rows(account: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("account_trades", "public_data_activity", "existing_activity"):
        values = account.get(key) or []
        if isinstance(values, list):
            rows.extend(item for item in values if isinstance(item, dict))
    return rows


def _known_btc_hashes(account: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return tx -> the existing BTC activity row used for market identity."""
    result: dict[str, dict[str, Any]] = {}
    for raw in _source_trade_rows(account):
        normalized = normalize_activity_row(raw)
        if normalized and is_btc_five_minute(normalized):
            tx = _tx(normalized["transaction_ref"])
            if tx:
                result.setdefault(tx, normalized)
    return dict(list(result.items())[:MAX_TRANSACTION_HASHES])


def _graphql_request(
    endpoint: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode()
    request = Request(
        endpoint,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "poly-trade-h308/1.0"},
        method="POST",
    )
    last = "graphql_request_failed"
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("errors"):
                    raise RuntimeError("graphql_response_error")
                if not isinstance(payload.get("data"), dict):
                    raise RuntimeError("graphql_data_missing")
                return payload
        except Exception as exc:
            last = type(exc).__name__
            if attempt + 1 == MAX_RETRIES:
                raise RuntimeError(last) from exc
            time.sleep(min(2**attempt, 4))
    raise RuntimeError(last)


def _subgraph_events(
    hashes: list[str],
    *,
    fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    endpoint: str,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    rows: list[dict[str, Any]] = []
    queries = 0
    failures: list[str] = []
    for start in range(0, len(hashes), MAX_HASHES_PER_QUERY):
        chunk = hashes[start : start + MAX_HASHES_PER_QUERY]
        queries += 1
        try:
            payload = fetcher(endpoint, EVENT_QUERY, {"hashes": chunk})
            data = payload.get("data") if isinstance(payload, dict) else None
            events = data.get("orderFilledEvents") if isinstance(data, dict) else None
            if not isinstance(events, list):
                raise ValueError("order_filled_events_missing")
            rows.extend(item for item in events if isinstance(item, dict))
        except Exception as exc:
            failures.append(type(exc).__name__)
    return rows, queries, failures


def _event_to_activity(event: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    tx = _tx(event.get("transactionHash"))
    if not tx:
        return None
    amount = _event_amount(event)
    timestamp = event.get("timestamp")
    if amount is None or timestamp in (None, ""):
        return None
    row = {
        "type": "TRADE",
        "side": _event_side(event),
        "transactionHash": tx,
        "timestamp": timestamp,
        "usdc_size": format(amount, "f"),
        "slug": source.get("slug", ""),
        "conditionId": source.get("condition_id", ""),
    }
    return normalize_activity_row(row)


def _receipt_join(
    owner: str,
    trades: list[dict[str, Any]],
    rpc_call: Callable[[str, list[Any]], Any],
) -> dict[str, Any]:
    condition_ids = {row["condition_id"] for row in trades if row.get("condition_id")}
    transfers: list[dict[str, Any]] = []
    payouts: list[dict[str, Any]] = []
    confirmed = legacy = v2 = unbound = failures = 0
    for tx in dict.fromkeys(row["transaction_ref"] for row in trades):
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx])
            if not isinstance(receipt, dict):
                raise ValueError("receipt_missing")
            old_transfers, old_payouts, old_confirmed = decode_receipt_logs(receipt, owner=owner)
            new_transfers, new_payouts, admitted, not_bound, new_confirmed = _v2_receipt_logs(
                receipt, owner=owner, condition_ids=condition_ids
            )
            transfers.extend(old_transfers + new_transfers)
            payouts.extend(old_payouts + new_payouts)
            legacy += len(old_payouts)
            v2 += admitted
            unbound += not_bound
            confirmed += int(bool(old_confirmed or new_confirmed))
        except Exception:
            failures += 1
    reconciliation = _reconcile(trades, transfers, payouts, owner)
    cash = sum(
        value.get("cash_release_transaction_rows", 0)
        for value in reconciliation.values()
        if isinstance(value, dict)
    )
    return {
        "confirmed_trade_receipts": confirmed,
        "receipt_fetch_failures": failures,
        "legacy_settlement_receipt_rows": legacy,
        "v2_settlement_receipt_rows": v2,
        "v2_unbound_payout_rows": unbound,
        "cash_release_receipt_rows": cash,
        "cash_reconciliation_by_collateral_token": reconciliation,
    }


def capture_account(
    account: dict[str, Any],
    *,
    fetcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    rpc_call: Callable[[str, list[Any]], Any],
    endpoint: str = SUBGRAPH_URL,
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    known = _known_btc_hashes(account)
    events, queries, failures = _subgraph_events(list(known), fetcher=fetcher, endpoint=endpoint)
    owner_events = [event for event in events if _lower_address(event.get("maker")) == owner]
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in owner_events:
        tx = _tx(event.get("transactionHash"))
        source = known.get(tx)
        if not source:
            continue
        row = _event_to_activity(event, source)
        key = (tx, _text(event.get("orderHash")))
        if row and key not in seen:
            seen.add(key)
            normalized.append(row)
    joined = _receipt_join(owner, normalized, rpc_call)
    return {
        "owner_address_sha256": _digest(owner),
        "known_btc_transaction_hashes": len(known),
        "subgraph_queries": queries,
        "subgraph_raw_event_rows": len(events),
        "subgraph_owner_event_rows": len(owner_events),
        "normalized_btc_trade_rows": len(normalized),
        "subgraph_failures": failures,
        **joined,
        "private_order_filled_events": owner_events,
        "private_normalized_btc_trades": normalized,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def capture(accounts: Iterable[dict[str, Any]], *, fetcher: Callable = _graphql_request, rpc_call: Callable, endpoint: str = SUBGRAPH_URL) -> dict[str, Any]:
    rows = list(accounts)
    if not rows or len(rows) > MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    results = [capture_account(row, fetcher=fetcher, rpc_call=rpc_call, endpoint=endpoint) for row in rows]
    return {
        "format": "h308-orderbook-subgraph-receipt-reconciliation-v1",
        "source": "official Polymarket orderbook subgraph OrderFilledEvent",
        "accounts": results,
        "new_data_collected": False,
        "model_refit": False,
        "orders_submitted": False,
        "settlement_or_redemption_submitted": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
        "evidence_boundary": "Indexed execution evidence remains non-Canary until owner settlement, cash release and reusable balance are independently reconciled.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", default=SUBGRAPH_URL)
    parser.add_argument("--rpc-url", required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    result = capture(
        payload.get("accounts", []),
        endpoint=args.endpoint,
        rpc_call=lambda method, params: _rpc_call(args.rpc_url, method, params),
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("format", "actual_fills", "verified_canary_fills", "same_ledger_compounding_rows")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
