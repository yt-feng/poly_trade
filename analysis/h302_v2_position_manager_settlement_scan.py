"""Bounded read-only capture for Polymarket V2 settlement events.

The legacy H301 scan covered ConditionalTokens ``PayoutRedemption`` only.
Polymarket's current V2 contracts route positions through module contracts and
emit ``PositionRedeemed`` or ``PositionsMerged`` with an owner recipient.  A
successful event is admitted only when its condition identity is linked to an
existing BTC five-minute activity row and the matching collateral transfer is
confirmed in the same transaction.  No transaction is signed or submitted.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from .h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        TRANSFER_TOPIC,
        _data_words,
        _hex_number,
        _lower_address,
        _owner_digest,
        _topic_address,
        _tx,
        _word,
        is_btc_five_minute,
        normalize_activity_row,
        decode_receipt_logs,
    )
    from .h301_owner_scoped_settlement_history_scan import (
        DEFAULT_CHUNK_BLOCKS,
        MAX_ACCOUNTS,
        MAX_HISTORY_BLOCKS,
        MAX_LOG_ROWS,
        MAX_LOG_TRANSACTIONS,
        MAX_SEED_TRANSACTIONS,
        _activity_condition_ids,
        _amount_usdc,
        _chunk_ranges,
        _hex_int,
        _log_filter,
        _normalize_log_rows,
        _reconcile,
        _rpc_call,
        _seed_transaction_hashes,
        _topic_address as _owner_topic,
    )
except ImportError:  # direct ``python analysis/h302_*.py`` execution
    from h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        TRANSFER_TOPIC,
        _data_words,
        _hex_number,
        _lower_address,
        _owner_digest,
        _topic_address,
        _tx,
        _word,
        is_btc_five_minute,
        normalize_activity_row,
        decode_receipt_logs,
    )
    from h301_owner_scoped_settlement_history_scan import (
        DEFAULT_CHUNK_BLOCKS,
        MAX_ACCOUNTS,
        MAX_HISTORY_BLOCKS,
        MAX_LOG_ROWS,
        MAX_LOG_TRANSACTIONS,
        MAX_SEED_TRANSACTIONS,
        _activity_condition_ids,
        _amount_usdc,
        _chunk_ranges,
        _hex_int,
        _log_filter,
        _normalize_log_rows,
        _reconcile,
        _rpc_call,
        _seed_transaction_hashes,
        _topic_address as _owner_topic,
    )


# Official Polymarket V2 Polygon deployment map (main branch of the public
# contract repository).  Events are emitted by the module proxies, while the
# collateral Transfer is emitted by the V2 collateral token.
V2_POSITION_MANAGER = "0x006f54f7f9a22e0000cc2ab60031000000ae9fef"
V2_MODULES = frozenset({
    "0x1000008dd9001b968442c1000017eae6e0da00ba",  # BinaryModule
    "0x200000900045e3b6259600682756002200028933",  # NegRiskModule
    "0x30000034706c7d8e12009dab006be20000c031a8",  # CombinatorialModule
})
V2_COLLATERAL_TOKEN = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
V2_COLLATERAL_TOKENS = frozenset(set(DEFAULT_COLLATERAL_TOKENS) | {V2_COLLATERAL_TOKEN})

# Keep the historical default conservative, but allow a separately registered
# wider read-only audit to cover the gap between old seed receipts and the
# latest block.  This is a query bound, never an admission or trading change.
MAX_HISTORY_BLOCKS_ALLOWED = 5_000_000
DEFAULT_MAX_LOG_QUERY_LEAVES = 16
MAX_LOG_QUERY_LEAVES_ALLOWED = 128
LOG_FILTER_MODES = frozenset({"owner_bound", "scalar_local_decode"})

# keccak256("PositionRedeemed(address,uint256,address,uint256,uint256)")
POSITION_REDEEMED_TOPIC = "0x4fd474b703f46b453536518b4cbc159dfbea6fd663233763d321a9bc8ccdf97f"
# keccak256("PositionsMerged(address,bytes31,address,uint256)")
POSITIONS_MERGED_TOPIC = "0xf233f7be4ce81130a78e8eb2fa626497586c56094d27bfec2e196471e9c60301"


def _failure_category(exc: Exception) -> str:
    """Return a bounded, non-sensitive category for a history query failure."""
    text = str(exc)
    match = re.fullmatch(r"history_rpc_error:([A-Za-z0-9_.:+-]{1,32})", text)
    if match:
        return f"rpc_error_{match.group(1)}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__.lower()[:32] or "unknown"


def _owner_relevant_log(log: dict[str, Any], owner: str) -> bool:
    """Keep owner-relevant rows after a provider-compatible broad filter.

    Some providers reject the JSON-RPC ``null`` wildcard needed to bind an
    indexed transfer recipient.  In ``scalar_local_decode`` mode we query a
    single contract and topic0, then apply the owner binding locally before
    the bounded history row cap.
    """
    expected_owner = _lower_address(owner)
    if not expected_owner:
        return False
    topics = log.get("topics") or []
    if not isinstance(topics, list) or not topics:
        return False
    topic0 = str(topics[0]).lower()
    if topic0 == TRANSFER_TOPIC and len(topics) >= 3:
        return _topic_address(topics[1]) == expected_owner or _topic_address(topics[2]) == expected_owner
    if topic0 in {POSITION_REDEEMED_TOPIC, POSITIONS_MERGED_TOPIC} and len(topics) >= 4:
        return _topic_address(topics[3]) == expected_owner
    return False


def _topic_word(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return "0x" + text if len(text) == 64 else ""


def _v2_condition_from_position(value: Any) -> str:
    position = _hex_int(value)
    if position is None:
        return ""
    # PositionId stores the canonical ConditionId in the high 31 bytes and
    # the outcome index in the low byte.
    return "0x" + f"{position & ~0xff:064x}"


def _split_log_filter(log_filter: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a provider-sensitive filter without changing its event meaning.

    Some public Polygon RPCs reject OR-valued address/topic filters with HTTP
    400.  The fallback first removes OR lists, then halves the block range so
    a successful narrow query can distinguish provider shape limits from an
    empty event set.  The caller bounds the number of leaves through the
    existing chunk and row limits.
    """
    topics = list(log_filter.get("topics") or [])
    topic_zero = topics[0] if topics else None
    if isinstance(topic_zero, list) and len(topic_zero) > 1:
        return [
            {**log_filter, "topics": [topic] + topics[1:]}
            for topic in topic_zero
        ]
    address = log_filter.get("address")
    if isinstance(address, list) and len(address) > 1:
        return [{**log_filter, "address": item} for item in address]
    try:
        lower = int(str(log_filter["fromBlock"]), 16)
        upper = int(str(log_filter["toBlock"]), 16)
    except (KeyError, TypeError, ValueError):
        return []
    if lower >= upper:
        return []
    middle = (lower + upper) // 2
    return [
        {**log_filter, "fromBlock": hex(lower), "toBlock": hex(middle)},
        {**log_filter, "fromBlock": hex(middle + 1), "toBlock": hex(upper)},
    ]


def _adaptive_get_logs(
    rpc_call: Callable[[str, list[Any]], Any],
    log_filter: dict[str, Any],
    *,
    max_leaves: int = DEFAULT_MAX_LOG_QUERY_LEAVES,
) -> tuple[list[dict[str, Any]], int]:
    """Read logs with bounded shape/range fallback and return (rows, splits)."""
    if not 1 <= int(max_leaves) <= MAX_LOG_QUERY_LEAVES_ALLOWED:
        raise ValueError("max_log_query_leaves_invalid")
    pending = [log_filter]
    rows: list[dict[str, Any]] = []
    splits = 0
    leaves = 0
    last_error: Exception | None = None
    while pending:
        current = pending.pop()
        try:
            rows.extend(_normalize_log_rows(rpc_call("eth_getLogs", [current])))
            leaves += 1
            continue
        except Exception as exc:
            last_error = exc
        parts = _split_log_filter(current)
        if not parts or leaves + len(pending) + len(parts) > max_leaves:
            raise last_error  # type: ignore[misc]
        splits += 1
        pending.extend(parts)
    # A provider may return the same log through multiple equivalent filters;
    # dedupe only on its stable log identity and preserve all other fields.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        topics = row.get("topics")
        key = (
            str(row.get("transactionHash") or row.get("transaction_hash") or "").lower(),
            str(row.get("logIndex") or row.get("log_index") or "").lower(),
            str(row.get("blockNumber") or row.get("block_number") or "").lower(),
            str(row.get("address") or "").lower(),
            tuple(str(item).lower() for item in topics) if isinstance(topics, list) else str(topics),
            str(row.get("data") or "").lower(),
        )
        unique.setdefault(key, row)
    return list(unique.values()), splits


def _v2_receipt_logs(
    receipt: dict[str, Any],
    *,
    owner: str,
    condition_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int, bool]:
    expected_owner = _lower_address(owner)
    if not expected_owner:
        raise ValueError("owner_address_required")
    status = str(receipt.get("status") or "").lower()
    confirmed = status in {"0x1", "1"}
    tx_hash = _tx(receipt.get("transactionHash") or receipt.get("transaction_hash"))
    if not tx_hash:
        raise ValueError("receipt_transaction_hash_required")
    block_number = _hex_number(receipt.get("blockNumber"))
    transfers: list[dict[str, Any]] = []
    payouts: list[dict[str, Any]] = []
    admitted_payouts = 0
    unbound_payouts = 0
    if not confirmed:
        return transfers, payouts, admitted_payouts, unbound_payouts, confirmed
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics") or []
        if not topics:
            continue
        topic0 = str(topics[0]).lower()
        contract = _lower_address(log.get("address"))
        if topic0 == TRANSFER_TOPIC and contract in V2_COLLATERAL_TOKENS and len(topics) >= 3:
            source = _topic_address(topics[1])
            target = _topic_address(topics[2])
            words = _data_words(log.get("data"))
            if source and target and len(words) == 1:
                try:
                    amount = _word(words[0])
                except ValueError:
                    continue
                if source == expected_owner or target == expected_owner:
                    transfers.append({
                        "transaction_ref": tx_hash,
                        "token": contract,
                        "from": source,
                        "to": target,
                        "amount_base_units": amount,
                        "confirmed_nonreverted": True,
                        "block_number": block_number,
                    })
            continue
        if contract not in V2_MODULES or len(topics) < 4:
            continue
        recipient = _topic_address(topics[3])
        if recipient != expected_owner:
            continue
        words = _data_words(log.get("data"))
        if topic0 == POSITION_REDEEMED_TOPIC:
            if len(words) < 2:
                continue
            condition_id = _v2_condition_from_position(topics[2])
            if condition_id not in condition_ids:
                unbound_payouts += 1
                continue
            try:
                amount = _word(words[0])
                payout = _word(words[1])
            except ValueError:
                continue
            payouts.append({
                "transaction_ref": tx_hash,
                "amount_base_units": payout,
                "collateral_token": V2_COLLATERAL_TOKEN,
                "condition_id": condition_id,
                "event_kind": "POSITION_REDEEMED",
                "position_amount_base_units": amount,
                "redeemer_address_sha256": _owner_digest(expected_owner),
                "confirmed_nonreverted": True,
                "block_number": block_number,
            })
            admitted_payouts += 1
        elif topic0 == POSITIONS_MERGED_TOPIC:
            if not words:
                continue
            condition_id = _topic_word(topics[2])
            if condition_id not in condition_ids:
                unbound_payouts += 1
                continue
            try:
                amount = _word(words[0])
            except ValueError:
                continue
            payouts.append({
                "transaction_ref": tx_hash,
                "amount_base_units": amount,
                "collateral_token": V2_COLLATERAL_TOKEN,
                "condition_id": condition_id,
                "event_kind": "POSITIONS_MERGED",
                "redeemer_address_sha256": _owner_digest(expected_owner),
                "confirmed_nonreverted": True,
                "block_number": block_number,
            })
            admitted_payouts += 1
    return transfers, payouts, admitted_payouts, unbound_payouts, confirmed


def capture_account(
    account: dict[str, Any],
    *,
    rpc_call: Callable[[str, list[Any]], Any],
    max_history_blocks: int = MAX_HISTORY_BLOCKS,
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
    max_log_query_leaves: int = DEFAULT_MAX_LOG_QUERY_LEAVES,
    log_filter_mode: str = "owner_bound",
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    if log_filter_mode not in LOG_FILTER_MODES:
        raise ValueError("log_filter_mode_invalid")
    raw_activity = account.get("activity") or []
    normalized = [item for raw in raw_activity if (item := normalize_activity_row(raw))]
    btc_rows = [item for item in normalized if is_btc_five_minute(item)]
    condition_ids = _activity_condition_ids(btc_rows)
    seed_hashes = _seed_transaction_hashes(account, btc_rows)
    transfer_rows: list[dict[str, Any]] = []
    payout_rows: list[dict[str, Any]] = []
    seed_blocks: list[int] = []
    seed_receipts = 0
    receipt_failures = 0
    block_timestamp_cache: dict[int, int] = {}

    def enrich(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            block = row.get("block_number")
            if block is None:
                continue
            if block not in block_timestamp_cache:
                try:
                    value = rpc_call("eth_getBlockByNumber", [hex(block), False])
                    timestamp = _hex_int(value.get("timestamp")) if isinstance(value, dict) else None
                    if timestamp is not None:
                        block_timestamp_cache[block] = timestamp
                except Exception:
                    continue
            if block in block_timestamp_cache:
                row["block_timestamp"] = block_timestamp_cache[block]

    for tx_hash in seed_hashes:
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict):
                raise ValueError("seed_receipt_missing")
            block = _hex_int(receipt.get("blockNumber"))
            if block is not None:
                seed_blocks.append(block)
            transfers, payouts, _, _, confirmed = _v2_receipt_logs(
                receipt, owner=owner, condition_ids=condition_ids
            )
            enrich(transfers + payouts)
            transfer_rows.extend(transfers)
            payout_rows.extend(payouts)
            seed_receipts += int(confirmed)
        except Exception:
            receipt_failures += 1
    if not seed_blocks:
        return {
            "label": str(account.get("label") or ""),
            "owner_address_sha256": _owner_digest(owner),
            "btc_five_minute_activity_rows": len(btc_rows),
            "condition_ids_available": len(condition_ids),
            "seed_transaction_hashes": len(seed_hashes),
            "seed_confirmed_receipts": seed_receipts,
            "history_scan_status": "no_seed_block_bounds",
            "settlement_receipt_rows": len(payout_rows),
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
    if not 1 <= int(max_log_query_leaves) <= MAX_LOG_QUERY_LEAVES_ALLOWED:
        raise ValueError("max_log_query_leaves_invalid")
    start = max(min(seed_blocks), latest_block - max_history_blocks + 1)
    history_truncated = start > min(seed_blocks)
    log_rows: list[dict[str, Any]] = []
    query_count = 0
    query_failures = 0
    query_recovery_splits = 0
    query_failure_categories: dict[str, int] = {}
    event_addresses = sorted(V2_MODULES)
    transfer_tokens = sorted(V2_COLLATERAL_TOKENS)
    for lower, upper in _chunk_ranges(start, latest_block, chunk_blocks):
        if log_filter_mode == "scalar_local_decode":
            # H514 established that scalar address + topic0 is accepted by
            # the configured providers, while the owner-bound null wildcard
            # is rejected.  Decode the owner from returned topics locally.
            filters = [
                _log_filter(lower, upper, address, [topic])
                for address in event_addresses
                for topic in (POSITION_REDEEMED_TOPIC, POSITIONS_MERGED_TOPIC)
            ]
            filters.extend(
                _log_filter(lower, upper, token, [TRANSFER_TOPIC])
                for token in transfer_tokens
            )
        else:
            filters = [
                # JSON-RPC permits OR lists in topic position 0 and address.
                # Combining equivalent filters preserves decoding semantics
                # for providers that accept the owner-bound wildcard form.
                _log_filter(lower, upper, event_addresses, [[POSITION_REDEEMED_TOPIC, POSITIONS_MERGED_TOPIC], None, None, _owner_topic(owner)]),
                _log_filter(lower, upper, transfer_tokens, [TRANSFER_TOPIC, None, _owner_topic(owner)]),
            ]
        for log_filter in filters:
            query_count += 1
            try:
                recovered_rows, splits = _adaptive_get_logs(
                    rpc_call, log_filter, max_leaves=max_log_query_leaves
                )
                if log_filter_mode == "scalar_local_decode":
                    recovered_rows = [row for row in recovered_rows if _owner_relevant_log(row, owner)]
                log_rows.extend(recovered_rows)
                query_recovery_splits += int(splits)
            except Exception as exc:
                query_failures += 1
                category = _failure_category(exc)
                query_failure_categories[category] = query_failure_categories.get(category, 0) + 1
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
    history_hashes = list(tx_logs)[:MAX_LOG_TRANSACTIONS]
    history_receipts = 0
    unbound_payout_rows = 0
    for tx_hash in history_hashes:
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict):
                raise ValueError("history_receipt_missing")
            transfers, payouts, admitted, unbound, confirmed = _v2_receipt_logs(
                receipt, owner=owner, condition_ids=condition_ids
            )
            if confirmed:
                history_receipts += 1
                enrich(transfers + payouts)
                transfer_rows.extend(transfers)
                payout_rows.extend(payouts)
                unbound_payout_rows += unbound
        except Exception:
            receipt_failures += 1

    reconciliations = _reconcile(btc_rows, transfer_rows, payout_rows, owner)
    cash_release_rows = sum(
        value.get("cash_release_transaction_rows", 0)
        for value in reconciliations.values()
        if isinstance(value, dict)
    )
    return {
        "label": str(account.get("label") or ""),
        "owner_address_sha256": _owner_digest(owner),
        "btc_five_minute_activity_rows": len(btc_rows),
        "condition_ids_available": len(condition_ids),
        "seed_transaction_hashes": len(seed_hashes),
        "seed_confirmed_receipts": seed_receipts,
        "seed_block_min": min(seed_blocks),
        "seed_block_max": max(seed_blocks),
        "history_block_from": start,
        "history_block_to": latest_block,
        "history_truncated": history_truncated,
        "history_log_rows": len(log_rows),
        "history_transaction_count": len(history_hashes),
        "history_query_count": query_count,
        "history_query_failures": query_failures,
        "history_query_recovery_splits": query_recovery_splits,
        "history_query_failure_categories": query_failure_categories,
        "history_log_filter_mode": log_filter_mode,
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
    max_log_query_leaves: int = DEFAULT_MAX_LOG_QUERY_LEAVES,
    log_filter_mode: str = "owner_bound",
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
            max_log_query_leaves=max_log_query_leaves,
            log_filter_mode=log_filter_mode,
        )
        for row in rows
    ]
    return {
        "format": "h302-v2-position-manager-settlement-scan-v1",
        "accounts": reports,
        "scope": "read-only bounded Polymarket V2 module and collateral history",
        "settlement_receipt_rows": sum(row.get("settlement_receipt_rows", 0) for row in reports),
        "cash_release_receipt_rows": sum(row.get("cash_release_receipt_rows", 0) for row in reports),
        "owner_scoped_full_settlement_cash_bundle": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "history_log_filter_mode": log_filter_mode,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument(
        "--receipt-rpc-url",
        default="",
        help="Optional read-only endpoint for transaction receipts; history RPC remains --rpc-url.",
    )
    parser.add_argument("--max-history-blocks", type=int, default=MAX_HISTORY_BLOCKS)
    parser.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK_BLOCKS)
    parser.add_argument("--max-log-query-leaves", type=int, default=DEFAULT_MAX_LOG_QUERY_LEAVES)
    parser.add_argument("--log-filter-mode", choices=sorted(LOG_FILTER_MODES), default="owner_bound")
    args = parser.parse_args()
    if not 1 <= args.max_history_blocks <= MAX_HISTORY_BLOCKS_ALLOWED:
        raise SystemExit(f"max-history-blocks must be between 1 and {MAX_HISTORY_BLOCKS_ALLOWED}")
    if not 1 <= args.chunk_blocks <= args.max_history_blocks:
        raise SystemExit("chunk-blocks must be positive and no larger than max-history-blocks")
    if not 1 <= args.max_log_query_leaves <= MAX_LOG_QUERY_LEAVES_ALLOWED:
        raise SystemExit(
            f"max-log-query-leaves must be between 1 and {MAX_LOG_QUERY_LEAVES_ALLOWED}"
        )
    document = json.loads(args.input.read_text(encoding="utf-8"))
    receipt_rpc_url = args.receipt_rpc_url.strip() or args.rpc_url

    def rpc_call(method: str, params: list[Any]) -> Any:
        # Some public providers support broad eth_getLogs ranges but fail on
        # transaction receipts (and the reverse is also observed).  Keep the
        # two read-only surfaces independently selectable without changing the
        # settlement admission logic.
        endpoint = receipt_rpc_url if method == "eth_getTransactionReceipt" else args.rpc_url
        return _rpc_call(endpoint, method, params)

    result = capture(
        document.get("accounts") or [],
        rpc_call=rpc_call,
        max_history_blocks=args.max_history_blocks,
        chunk_blocks=args.chunk_blocks,
        max_log_query_leaves=args.max_log_query_leaves,
        log_filter_mode=args.log_filter_mode,
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
