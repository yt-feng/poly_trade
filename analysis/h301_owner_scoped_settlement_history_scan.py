"""Bounded read-only scan for settlement transactions separate from fills.

H300 deliberately inspected only the transaction hashes already attached to
authenticated BTC five-minute activity.  A redemption can happen in a later
transaction, so this module uses the existing receipt block bounds, scans a
bounded topic/address history, confirms every candidate transaction receipt,
and admits only payouts whose ConditionalTokens ``conditionId`` is present in
the BTC five-minute activity set.  It does not sign, submit, redeem, merge,
transfer, change allowances, read balances, or count a settlement as a fill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from .h284_owner_scoped_cash_reconciliation import reconcile_observed_cash
    from .h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        DEFAULT_CTF_CONTRACTS,
        PAYOUT_REDEMPTION_TOPIC,
        TRANSFER_TOPIC,
        _lower_address,
        _owner_digest,
        _tx,
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
    )
except ImportError:  # direct ``python analysis/h301_*.py`` execution
    from h284_owner_scoped_cash_reconciliation import reconcile_observed_cash
    from h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        DEFAULT_CTF_CONTRACTS,
        PAYOUT_REDEMPTION_TOPIC,
        TRANSFER_TOPIC,
        _lower_address,
        _owner_digest,
        _tx,
        decode_receipt_logs,
        is_btc_five_minute,
        normalize_activity_row,
    )


MAX_ACCOUNTS = 4
MAX_ACTIVITY_ROWS = 400
MAX_SEED_TRANSACTIONS = 200
MAX_HISTORY_BLOCKS = 250_000
MAX_LOG_ROWS = 600
MAX_LOG_TRANSACTIONS = 200
DEFAULT_CHUNK_BLOCKS = 5_000
RPC_MAX_ATTEMPTS = 3
RPC_RETRY_BACKOFF_SECONDS = 0.25
TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
HEX = re.compile(r"^[0-9a-fA-F]+$")
BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _hex_int(value: Any) -> int | None:
    text = _text(value).lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or not HEX.fullmatch(text):
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _topic_address(address: str) -> str:
    owner = _lower_address(address)
    return "0x" + ("0" * 24) + owner[2:] if owner else ""


def _condition_id(value: Any) -> str:
    text = _text(value).lower()
    if BYTES32.fullmatch(text):
        return text
    return ""


def _amount_usdc(amount_base_units: int) -> str:
    return format(Decimal(amount_base_units) / Decimal(1_000_000), "f")


def _first(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(RPC_MAX_ATTEMPTS):
        request = Request(
            rpc_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "poly-trade-h301/1.0",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in TRANSIENT_HTTP_CODES and attempt + 1 < RPC_MAX_ATTEMPTS:
                time.sleep(RPC_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise RuntimeError(f"history_rpc_error:http_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            if attempt + 1 < RPC_MAX_ATTEMPTS:
                time.sleep(RPC_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise RuntimeError("history_rpc_error:transport") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            if attempt + 1 < RPC_MAX_ATTEMPTS:
                time.sleep(RPC_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise RuntimeError("history_rpc_error:invalid_json") from None

        if payload.get("error") or "result" not in payload:
            # Preserve only a bounded provider error code.  The full provider
            # message can contain request details and is never written to the
            # public/redacted report.
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            code_text = str(code)[:32] if isinstance(code, (int, str)) else ""
            transient = code in TRANSIENT_HTTP_CODES or code_text in {str(item) for item in TRANSIENT_HTTP_CODES}
            if transient and attempt + 1 < RPC_MAX_ATTEMPTS:
                time.sleep(RPC_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            if code_text:
                raise RuntimeError(f"history_rpc_error:{code_text}")
            raise RuntimeError("history_rpc_error")
        return payload["result"]
    raise RuntimeError("history_rpc_error:retry_exhausted")


def _activity_condition_ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        raw = _first(row, ("condition_id", "conditionId", "conditionID", "market"))
        condition = _condition_id(raw)
        if condition:
            result.add(condition)
    return result


def _seed_transaction_hashes(account: dict[str, Any], btc_rows: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    explicit = account.get("btc_transaction_hashes") or account.get("transaction_hashes") or []
    if isinstance(explicit, list):
        values.extend(_tx(item) for item in explicit)
    values.extend(row["transaction_ref"] for row in btc_rows)
    return list(dict.fromkeys(value for value in values if value))[:MAX_SEED_TRANSACTIONS]


def _chunk_ranges(start: int, end: int, chunk_blocks: int) -> Iterable[tuple[int, int]]:
    current = start
    while current <= end:
        upper = min(end, current + chunk_blocks - 1)
        yield current, upper
        current = upper + 1


def _log_filter(from_block: int, to_block: int, address: str | list[str], topics: list[Any]) -> dict[str, Any]:
    return {
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": address,
        "topics": topics,
    }


def _normalize_log_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _reconcile(
    btc_rows: list[dict[str, Any]],
    transfer_rows: list[dict[str, Any]],
    payout_rows: list[dict[str, Any]],
    owner: str,
) -> dict[str, dict[str, Any]]:
    if not btc_rows:
        return {}
    activity = [
        {key: row[key] for key in ("type", "side", "transaction_ref", "timestamp", "usdc_size")}
        for row in btc_rows
    ]
    existing_redeems = {row["transaction_ref"] for row in btc_rows if row["type"] == "REDEEM"}
    # A later payout transaction is added as a synthetic activity row solely
    # to build the chronological cash path.  Its timestamp is explicitly a
    # block-order proxy; it never becomes an execution fill.
    for payout in payout_rows:
        tx = payout["transaction_ref"]
        if tx in existing_redeems:
            continue
        block_timestamp = payout.get("block_timestamp")
        if block_timestamp is None:
            continue
        activity.append({
            "type": "REDEEM",
            "side": "",
            "transaction_ref": tx,
            "timestamp": block_timestamp,
            "usdc_size": _amount_usdc(int(payout["amount_base_units"])),
        })
    tokens = sorted({row["token"] for row in transfer_rows} | {row["collateral_token"] for row in payout_rows})
    result: dict[str, dict[str, Any]] = {}
    for token in tokens:
        try:
            result[token] = reconcile_observed_cash(
                activity,
                transfer_rows,
                owner=owner,
                collateral_token=token,
                payout_receipts=[row for row in payout_rows if row["collateral_token"] == token],
            )
        except Exception as exc:
            result[token] = {"error": type(exc).__name__}
    return result


def capture_account(
    account: dict[str, Any],
    *,
    rpc_call: Callable[[str, list[Any]], Any],
    max_history_blocks: int = MAX_HISTORY_BLOCKS,
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    raw_activity = account.get("activity") or []
    normalized = [item for raw in raw_activity if (item := normalize_activity_row(raw))]
    btc_rows = [item for item in normalized if is_btc_five_minute(item)]
    condition_ids = _activity_condition_ids(btc_rows)
    seed_hashes = _seed_transaction_hashes(account, btc_rows)
    seed_transfer_rows: list[dict[str, Any]] = []
    seed_payout_rows: list[dict[str, Any]] = []
    seed_blocks: list[int] = []
    seed_receipts = 0
    receipt_failures = 0
    block_timestamp_cache: dict[int, int] = {}

    def enrich_payouts(payouts: list[dict[str, Any]]) -> None:
        for payout in payouts:
            block_number = payout.get("block_number")
            if block_number is None or block_number in block_timestamp_cache:
                if block_number in block_timestamp_cache:
                    payout["block_timestamp"] = block_timestamp_cache[block_number]
                continue
            try:
                block = rpc_call("eth_getBlockByNumber", [hex(block_number), False])
                timestamp = _hex_int(block.get("timestamp")) if isinstance(block, dict) else None
            except Exception:
                timestamp = None
            if timestamp is not None:
                block_timestamp_cache[block_number] = timestamp
                payout["block_timestamp"] = timestamp

    for tx_hash in seed_hashes:
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict):
                raise ValueError("receipt_missing")
            block_number = _hex_int(receipt.get("blockNumber"))
            if block_number is not None:
                seed_blocks.append(block_number)
            transfers, payouts, confirmed = decode_receipt_logs(receipt, owner=owner)
            seed_transfer_rows.extend(transfers)
            enrich_payouts(payouts)
            seed_payout_rows.extend(payouts)
            seed_receipts += int(confirmed)
        except Exception:
            receipt_failures += 1

    if not seed_blocks:
        return {
            "label": _text(account.get("label")),
            "owner_address_sha256": _owner_digest(owner),
            "btc_five_minute_activity_rows": len(btc_rows),
            "condition_ids_available": len(condition_ids),
            "seed_transaction_hashes": len(seed_hashes),
            "seed_confirmed_receipts": seed_receipts,
            "history_scan_status": "no_seed_block_bounds",
            "history_log_rows": 0,
            "history_transaction_count": 0,
            "history_receipt_fetch_failures": receipt_failures,
            "settlement_receipt_rows": 0,
            "cash_release_receipt_rows": 0,
            "owner_scoped_full_settlement_cash_bundle": False,
            "cash_reconciliation_by_collateral_token": {},
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
            "live_orders_enabled": False,
        }

    latest_block = _hex_int(rpc_call("eth_blockNumber", []))
    if latest_block is None:
        raise ValueError("latest_block_unavailable")
    if max_history_blocks < 1 or chunk_blocks < 1:
        raise ValueError("history_bounds_invalid")
    min_seed_block = min(seed_blocks)
    from_block = max(min_seed_block, latest_block - max_history_blocks + 1)
    history_truncated = from_block > min_seed_block
    log_rows: list[dict[str, Any]] = []
    query_count = 0
    query_failures = 0
    contracts = sorted(DEFAULT_CTF_CONTRACTS)
    tokens = sorted(DEFAULT_COLLATERAL_TOKENS)
    # A delegated CtfCollateralAdapter calls ConditionalTokens on the
    # owner's behalf.  In that receipt PayoutRedemption.topic1 is the adapter
    # address, while the owner binding is established by the same receipt's
    # owner-directed pUSD mint in decode_receipt_logs.  Filtering topic1 to the
    # owner therefore drops valid delegated redemptions before decoding.  Use
    # the condition's collateral token as the bounded source filter and leave
    # the redeemer topic open; decode_receipt_logs still requires the exact
    # owner-directed cash leg and the BTC condition id before admission.
    collateral_topics = [_topic_address(token) for token in tokens]
    for start, end in _chunk_ranges(from_block, latest_block, chunk_blocks):
        filters = [
            _log_filter(start, end, contracts, [PAYOUT_REDEMPTION_TOPIC, None, collateral_topics]),
        ]
        # Only inbound collateral is scanned for separate cash release. Seed
        # trade receipts already supply owner outbound/inbound deltas.
        filters.extend(
            _log_filter(start, end, token, [TRANSFER_TOPIC, None, _topic_address(owner)])
            for token in tokens
        )
        for log_filter in filters:
            query_count += 1
            try:
                log_rows.extend(_normalize_log_rows(rpc_call("eth_getLogs", [log_filter])))
            except Exception:
                query_failures += 1
            if len(log_rows) >= MAX_LOG_ROWS:
                log_rows = log_rows[:MAX_LOG_ROWS]
                break
        if len(log_rows) >= MAX_LOG_ROWS:
            break

    tx_logs: dict[str, list[dict[str, Any]]] = {}
    for log in log_rows:
        tx_hash = _tx(log.get("transactionHash") or log.get("transaction_hash"))
        if tx_hash:
            tx_logs.setdefault(tx_hash, []).append(log)
    history_tx_hashes = list(tx_logs)[:MAX_LOG_TRANSACTIONS]
    history_transfer_rows: list[dict[str, Any]] = []
    history_payout_rows: list[dict[str, Any]] = []
    history_receipts = 0
    for tx_hash in history_tx_hashes:
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict):
                raise ValueError("history_receipt_missing")
            transfers, payouts, confirmed = decode_receipt_logs(receipt, owner=owner)
            if confirmed:
                history_receipts += 1
                history_transfer_rows.extend(transfers)
                enrich_payouts(payouts)
                history_payout_rows.extend(
                    payout for payout in payouts
                    if condition_ids and payout.get("condition_id") in condition_ids
                )
        except Exception:
            receipt_failures += 1

    unbound_payout_rows = 0
    if condition_ids:
        for tx_hash in history_tx_hashes:
            try:
                receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
                if not isinstance(receipt, dict):
                    continue
                _, payouts, confirmed = decode_receipt_logs(receipt, owner=owner)
                if confirmed:
                    unbound_payout_rows += sum(
                        payout.get("condition_id") not in condition_ids for payout in payouts
                    )
            except Exception:
                pass

    transfer_rows = seed_transfer_rows + history_transfer_rows
    payout_rows = [
        payout for payout in (seed_payout_rows + history_payout_rows)
        if condition_ids and payout.get("condition_id") in condition_ids
    ]
    reconciliations = _reconcile(btc_rows, transfer_rows, payout_rows, owner)
    cash_release_rows = sum(
        reconciliation.get("cash_release_transaction_rows", 0)
        for reconciliation in reconciliations.values()
        if isinstance(reconciliation, dict)
    )
    return {
        "label": _text(account.get("label")),
        "owner_address_sha256": _owner_digest(owner),
        "btc_five_minute_activity_rows": len(btc_rows),
        "condition_ids_available": len(condition_ids),
        "seed_transaction_hashes": len(seed_hashes),
        "seed_confirmed_receipts": seed_receipts,
        "seed_block_min": min_seed_block,
        "seed_block_max": max(seed_blocks),
        "history_block_from": from_block,
        "history_block_to": latest_block,
        "history_truncated": history_truncated,
        "history_log_rows": len(log_rows),
        "history_transaction_count": len(history_tx_hashes),
        "history_query_count": query_count,
        "history_query_failures": query_failures,
        "history_confirmed_receipts": history_receipts,
        "history_receipt_fetch_failures": receipt_failures,
        "unbound_payout_rows": unbound_payout_rows,
        "settlement_receipt_rows": len(payout_rows),
        "cash_release_receipt_rows": cash_release_rows,
        "owner_scoped_full_settlement_cash_bundle": False,
        "cash_reconciliation_by_collateral_token": reconciliations,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def capture(
    accounts: Iterable[dict[str, Any]],
    *,
    rpc_call: Callable[[str, list[Any]], Any],
    max_history_blocks: int = MAX_HISTORY_BLOCKS,
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    reports = [
        capture_account(
            row,
            rpc_call=rpc_call,
            max_history_blocks=max_history_blocks,
            chunk_blocks=chunk_blocks,
        )
        for row in rows
    ]
    return {
        "format": "h301-owner-scoped-settlement-history-scan-v1",
        "accounts": reports,
        "scope": "read-only bounded owner/topic/block-range settlement history",
        "settlement_receipt_rows": sum(row.get("settlement_receipt_rows", 0) for row in reports),
        "cash_release_receipt_rows": sum(row.get("cash_release_receipt_rows", 0) for row in reports),
        "owner_scoped_full_settlement_cash_bundle": False,
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
    parser.add_argument("--max-history-blocks", type=int, default=MAX_HISTORY_BLOCKS)
    parser.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK_BLOCKS)
    args = parser.parse_args()
    if not 1 <= args.max_history_blocks <= MAX_HISTORY_BLOCKS:
        raise SystemExit(f"max-history-blocks must be between 1 and {MAX_HISTORY_BLOCKS}")
    if not 1 <= args.chunk_blocks <= args.max_history_blocks:
        raise SystemExit("chunk-blocks must be positive and no larger than max-history-blocks")
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = capture(
        document.get("accounts") or [],
        rpc_call=lambda method, params: _rpc_call(args.rpc_url, method, params),
        max_history_blocks=args.max_history_blocks,
        chunk_blocks=args.chunk_blocks,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "computed",
        "format": result["format"],
        "settlement_receipt_rows": result["settlement_receipt_rows"],
        "cash_release_receipt_rows": result["cash_release_receipt_rows"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
