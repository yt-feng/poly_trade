"""Bounded, read-only Polygon OrderFilled probe.

This module talks only to a JSON-RPC endpoint for ``eth_getLogs`` and block
headers.  It has no wallet, signing, balance, order, cancel, or redemption
capability.  The default contract addresses and V1 topic come from the
official Polymarket subgraph network configuration; callers may supply an
additional address/topic for a separately verified V2 deployment.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


POLYGON_RPC_URL = "https://polygon-rpc.com"
V1_ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
V1_EXCHANGE_ADDRESSES = (
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
)
FINALITY_BUFFER_BLOCKS = 256
DEFAULT_WINDOW_BLOCKS = 2048
DEFAULT_CHUNK_BLOCKS = 200
DEFAULT_MAX_LOGS = 2000


def _hex_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        result = int(str(value), 16) if str(value).lower().startswith("0x") else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{field} must be nonnegative")
    return result


def _word(data: str, index: int) -> int:
    text = str(data)
    if not text.startswith("0x"):
        raise ValueError("log data must be hex")
    start = 2 + index * 64
    value = text[start:start + 64]
    if len(value) != 64:
        raise ValueError("missing OrderFilled data word")
    return int(value, 16)


def _address_topic(value: str) -> str:
    text = str(value)
    if not text.startswith("0x") or len(text) != 66:
        raise ValueError("indexed address topic must be 32-byte hex")
    return "0x" + text[-40:].lower()


def _topic(value: Any) -> str:
    text = str(value).lower()
    if not text.startswith("0x") or len(text) != 66:
        raise ValueError("event topic must be 32-byte hex")
    return text


def decode_v1_log(log: dict[str, Any], *, block_timestamp: int | None = None) -> dict[str, Any]:
    """Decode one V1 ``OrderFilled`` log into the normalizer's input shape."""
    if not isinstance(log, dict):
        raise ValueError("log must be an object")
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) < 4 or _topic(topics[0]) != V1_ORDER_FILLED_TOPIC:
        raise ValueError("not a V1 OrderFilled log")
    tx_hash = str(log.get("transactionHash") or "").lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError("transactionHash is required")
    block_number = _hex_int(log.get("blockNumber"), "blockNumber")
    log_index = _hex_int(log.get("logIndex"), "logIndex")
    maker_asset_id = _word(log.get("data", ""), 0)
    taker_asset_id = _word(log.get("data", ""), 1)
    maker_amount = _word(log.get("data", ""), 2)
    taker_amount = _word(log.get("data", ""), 3)
    fee = _word(log.get("data", ""), 4)
    result = {
        "exchange_version": "v1",
        "exchange_address": str(log.get("address") or "").lower(),
        "event_id": f"{tx_hash}:{log_index}",
        "tx_hash": tx_hash,
        "log_index": log_index,
        "block_number": block_number,
        "block_timestamp": block_timestamp,
        "timestamp_ms": None if block_timestamp is None else block_timestamp * 1000,
        "order_hash": _topic(topics[1]),
        "maker": _address_topic(topics[2]),
        "taker": _address_topic(topics[3]),
        "maker_asset_id": str(maker_asset_id),
        "taker_asset_id": str(taker_asset_id),
        "maker_amount_filled": str(maker_amount),
        "taker_amount_filled": str(taker_amount),
        "fee": str(fee),
    }
    if any(result[key] == "" for key in ("exchange_address", "maker", "taker")):
        raise ValueError("required OrderFilled identity is missing")
    return result


def decode_v2_log(log: dict[str, Any], *, topic: str, block_timestamp: int | None = None) -> dict[str, Any]:
    """Decode a separately verified V2 event with explicit side/token id."""
    if not isinstance(log, dict):
        raise ValueError("log must be an object")
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) < 4 or _topic(topics[0]) != _topic(topic):
        raise ValueError("not the supplied V2 OrderFilled topic")
    tx_hash = str(log.get("transactionHash") or "").lower()
    block_number = _hex_int(log.get("blockNumber"), "blockNumber")
    log_index = _hex_int(log.get("logIndex"), "logIndex")
    data = log.get("data", "")
    result = {
        "exchange_version": "v2",
        "exchange_address": str(log.get("address") or "").lower(),
        "event_id": f"{tx_hash}:{log_index}",
        "tx_hash": tx_hash,
        "log_index": log_index,
        "block_number": block_number,
        "block_timestamp": block_timestamp,
        "timestamp_ms": None if block_timestamp is None else block_timestamp * 1000,
        "order_hash": _topic(topics[1]),
        "maker": _address_topic(topics[2]),
        "taker": _address_topic(topics[3]),
        "side": str(_word(data, 0)),
        "token_id": str(_word(data, 1)),
        "maker_amount_filled": str(_word(data, 2)),
        "taker_amount_filled": str(_word(data, 3)),
        "fee": str(_word(data, 4)),
        "builder": "0x" + data[2 + 5 * 64:2 + 6 * 64] if len(data) >= 2 + 6 * 64 else None,
        "metadata": "0x" + data[2 + 6 * 64:2 + 7 * 64] if len(data) >= 2 + 7 * 64 else None,
    }
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError("transactionHash is required")
    if not result["exchange_address"]:
        raise ValueError("exchange address is required")
    return result


def _rpc(url: str, method: str, params: list[Any], request_id: int) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("polygon_rpc_transport_failed") from exc
    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise RuntimeError("polygon_rpc_error")
    return payload.get("result")


def _block_timestamp(url: str, block_number: int) -> int:
    result = _rpc(url, "eth_getBlockByNumber", [hex(block_number), False], block_number)
    if not isinstance(result, dict):
        raise RuntimeError("block_header_missing")
    return _hex_int(result.get("timestamp"), "block timestamp")


def probe(
    *,
    rpc_url: str = POLYGON_RPC_URL,
    from_block: int | None = None,
    to_block: int | None = None,
    window_blocks: int = DEFAULT_WINDOW_BLOCKS,
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
    max_logs: int = DEFAULT_MAX_LOGS,
    addresses: Iterable[str] = V1_EXCHANGE_ADDRESSES,
) -> dict[str, Any]:
    """Fetch a bounded finalized log window and return decoded records."""
    if window_blocks <= 0 or chunk_blocks <= 0 or max_logs <= 0:
        raise ValueError("probe bounds must be positive")
    latest = _hex_int(_rpc(rpc_url, "eth_blockNumber", [], 1), "latest block")
    finalized_raw = _rpc(rpc_url, "eth_getBlockByNumber", ["finalized", False], 2)
    if not isinstance(finalized_raw, dict):
        raise RuntimeError("finalized_block_unavailable")
    finalized = _hex_int(finalized_raw.get("number"), "finalized block")
    safe_to = finalized - FINALITY_BUFFER_BLOCKS
    if safe_to < 0:
        raise RuntimeError("finalized_block_too_low")
    requested_to = safe_to if to_block is None else min(int(to_block), safe_to)
    requested_from = max(0, requested_to - window_blocks + 1) if from_block is None else int(from_block)
    if requested_from < 0 or requested_from > requested_to:
        raise ValueError("invalid block range")
    addresses_list = tuple(sorted({str(address).lower() for address in addresses if str(address)}))
    if not addresses_list:
        raise ValueError("at least one exchange address is required")
    logs: dict[str, dict[str, Any]] = {}
    chunks = 0
    for address in addresses_list:
        start = requested_from
        while start <= requested_to:
            end = min(start + chunk_blocks - 1, requested_to)
            result = _rpc(
                rpc_url,
                "eth_getLogs",
                [{"address": address, "topics": [V1_ORDER_FILLED_TOPIC], "fromBlock": hex(start), "toBlock": hex(end)}],
                chunks + 10,
            )
            chunks += 1
            if not isinstance(result, list):
                raise RuntimeError("eth_getLogs_result_invalid")
            for log in result:
                decoded = decode_v1_log(log)
                logs[decoded["event_id"]] = decoded
                if len(logs) > max_logs:
                    raise RuntimeError("max_logs_exceeded")
            start = end + 1
    by_block: dict[int, int] = {}
    for item in logs.values():
        by_block.setdefault(int(item["block_number"]), None)
    for block_number in sorted(by_block):
        by_block[block_number] = _block_timestamp(rpc_url, block_number)
    for item in logs.values():
        timestamp = by_block[int(item["block_number"])]
        item["block_timestamp"] = timestamp
        item["timestamp_ms"] = timestamp * 1000
    records = [logs[key] for key in sorted(logs)]
    return {
        "schema_version": 1,
        "source": "polygon_json_rpc_eth_getLogs",
        "rpc_url": rpc_url,
        "latest_block_observed": latest,
        "finalized_block": finalized,
        "finality_buffer_blocks": FINALITY_BUFFER_BLOCKS,
        "from_block": requested_from,
        "to_block": requested_to,
        "addresses": list(addresses_list),
        "topic": V1_ORDER_FILLED_TOPIC,
        "chunk_blocks": chunk_blocks,
        "rpc_log_chunk_count": chunks,
        "record_count": len(records),
        "records": records,
        "authoritative_event_source": True,
        "account_scope": "public_all_accounts; not a private wallet receipt",
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-url", default=POLYGON_RPC_URL)
    parser.add_argument("--from-block", type=int)
    parser.add_argument("--to-block", type=int)
    parser.add_argument("--window-blocks", type=int, default=DEFAULT_WINDOW_BLOCKS)
    parser.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK_BLOCKS)
    parser.add_argument("--max-logs", type=int, default=DEFAULT_MAX_LOGS)
    args = parser.parse_args()
    result = probe(
        rpc_url=args.rpc_url, from_block=args.from_block, to_block=args.to_block,
        window_blocks=args.window_blocks, chunk_blocks=args.chunk_blocks, max_logs=args.max_logs,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("from_block", "to_block", "record_count", "finalized_block", "live_orders_enabled")}, sort_keys=True))


if __name__ == "__main__":
    main()

