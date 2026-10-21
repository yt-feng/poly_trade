"""H514: identify the smallest accepted Polygon ``eth_getLogs`` filter.

H513 showed ``-32602`` for every owner-bound scalar/OR shape on two
providers.  This probe changes no market-data or trading state.  It compares
provider support for minimal range, topic, address, and block-hash filters
before adding the owner topic.  Only bounded response categories and row
counts are written to the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

try:
    from .h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        TRANSFER_TOPIC,
        _lower_address,
        _owner_digest,
        _topic_address,
    )
    from .h301_owner_scoped_settlement_history_scan import _hex_int, _rpc_call
except ImportError:  # direct script execution
    from h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        TRANSFER_TOPIC,
        _lower_address,
        _owner_digest,
        _topic_address,
    )
    from h301_owner_scoped_settlement_history_scan import _hex_int, _rpc_call


FORMAT = "h514-rpc-minimal-filter-probe-v1"
DEFAULT_BLOCK_SPAN = 20
MAX_BLOCK_SPAN = 1000


def _classify(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("history_rpc_error:"):
        return text.split(":", 1)[1][:32]
    return type(exc).__name__.lower()[:32] or "unknown"


def _shape_filters(
    *,
    lower: int,
    upper: int,
    block_hash: str | None,
    owner: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    token = sorted(DEFAULT_COLLATERAL_TOKENS)[0]
    base = {"fromBlock": hex(lower), "toBlock": hex(upper)}
    shapes: list[tuple[str, dict[str, Any]]] = [
        ("range_empty", dict(base)),
        ("range_topic0", {**base, "topics": [TRANSFER_TOPIC]}),
        ("range_address", {**base, "address": token}),
        ("range_address_topic0", {**base, "address": token, "topics": [TRANSFER_TOPIC]}),
    ]
    if block_hash:
        shapes.extend(
            [
                ("blockhash_empty", {"blockHash": block_hash}),
                ("blockhash_topic0", {"blockHash": block_hash, "topics": [TRANSFER_TOPIC]}),
                ("blockhash_address_topic0", {"blockHash": block_hash, "address": token, "topics": [TRANSFER_TOPIC]}),
            ]
        )
    if owner:
        owner_topic = _topic_address(owner)
        shapes.extend(
            [
                ("owner_range_topic0", {**base, "topics": [TRANSFER_TOPIC, None, owner_topic]}),
                ("owner_range_address_topic0", {**base, "address": token, "topics": [TRANSFER_TOPIC, None, owner_topic]}),
            ]
        )
    return shapes


def _probe_endpoint(
    endpoint: str,
    accounts: list[dict[str, Any]],
    *,
    block_span: int,
    rpc_call: Callable[[str, str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    endpoint_sha256 = hashlib.sha256(endpoint.encode()).hexdigest()

    def call(method: str, params: list[Any]) -> Any:
        return rpc_call(endpoint, method, params) if rpc_call else _rpc_call(endpoint, method, params)

    try:
        latest = _hex_int(call("eth_blockNumber", []))
    except Exception as exc:
        return {"endpoint_sha256": endpoint_sha256, "block_number": None, "error": _classify(exc), "accounts": []}
    if latest is None:
        return {"endpoint_sha256": endpoint_sha256, "block_number": None, "error": "latest_block_unavailable", "accounts": []}
    lower = max(0, latest - block_span + 1)
    block_hash = None
    try:
        block = call("eth_getBlockByNumber", [hex(latest), False])
        candidate = block.get("hash") if isinstance(block, dict) else None
        if isinstance(candidate, str) and len(candidate) == 66 and candidate.startswith("0x"):
            block_hash = candidate.lower()
    except Exception:
        block_hash = None
    reports: list[dict[str, Any]] = []
    for account in accounts:
        owner = _lower_address(account.get("owner"))
        owner_reports = []
        categories: dict[str, int] = {}
        for shape, log_filter in _shape_filters(lower=lower, upper=latest, block_hash=block_hash, owner=owner or None):
            try:
                value = call("eth_getLogs", [log_filter])
                row_count = len(value) if isinstance(value, list) else 0
                category = "ok"
            except Exception as exc:
                row_count = 0
                category = _classify(exc)
            categories[category] = categories.get(category, 0) + 1
            owner_reports.append({"shape": shape, "category": category, "row_count": row_count})
        reports.append({"owner_address_sha256": _owner_digest(owner) if owner else None, "shape_reports": owner_reports, "categories": categories})
    return {
        "endpoint_sha256": endpoint_sha256,
        "block_number": latest,
        "block_span": block_span,
        "block_hash_query_succeeded": block_hash is not None,
        "accounts": reports,
    }


def probe(input_path: Path, endpoints: list[str], output: Path, *, block_span: int = DEFAULT_BLOCK_SPAN) -> dict[str, Any]:
    if not endpoints:
        raise ValueError("at_least_one_endpoint_required")
    if not 1 <= int(block_span) <= MAX_BLOCK_SPAN:
        raise ValueError("block_span_out_of_bounds")
    document = json.loads(input_path.read_text(encoding="utf-8"))
    accounts = [row for row in document.get("accounts") or [] if isinstance(row, dict)]
    reports = [_probe_endpoint(url.strip(), accounts, block_span=block_span) for url in endpoints if url.strip()]
    result = {
        "format": FORMAT,
        "accounts": len(accounts),
        "endpoint_count": len(reports),
        "reports": reports,
        "block_span": block_span,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "minimal read-only RPC filter-shape probe; response categories do not establish settlement or absence",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": FORMAT, "endpoint_count": len(reports), "accounts": len(accounts)}, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-urls", required=True)
    parser.add_argument("--block-span", type=int, default=DEFAULT_BLOCK_SPAN)
    args = parser.parse_args()
    probe(args.input, [value for value in args.rpc_urls.split(",")], args.output, block_span=args.block_span)


if __name__ == "__main__":
    main()
