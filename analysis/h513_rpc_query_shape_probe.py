"""H513: tiny read-only Polygon RPC query-shape compatibility probe.

The probe reuses existing authenticated owner activity only to obtain owner
topics and condition scope.  It does not scan history, submit transactions,
or publish addresses/logs.  It compares OR-valued and scalar filters over a
tiny recent block range and records only response categories and row counts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
    from .h302_v2_position_manager_settlement_scan import (
        POSITION_REDEEMED_TOPIC,
        POSITIONS_MERGED_TOPIC,
        V2_COLLATERAL_TOKEN,
        V2_MODULES,
    )
except ImportError:  # direct script execution
    from h300_owner_scoped_payout_transfer_capture import (
        DEFAULT_COLLATERAL_TOKENS,
        TRANSFER_TOPIC,
        _lower_address,
        _owner_digest,
        _topic_address,
    )
    from h301_owner_scoped_settlement_history_scan import _hex_int, _rpc_call
    from h302_v2_position_manager_settlement_scan import (
        POSITION_REDEEMED_TOPIC,
        POSITIONS_MERGED_TOPIC,
        V2_COLLATERAL_TOKEN,
        V2_MODULES,
    )


FORMAT = "h513-rpc-query-shape-probe-v1"
DEFAULT_BLOCK_SPAN = 20
MAX_BLOCK_SPAN = 1000
V2_COLLATERAL_TOKENS = frozenset(set(DEFAULT_COLLATERAL_TOKENS) | {V2_COLLATERAL_TOKEN})


def _classify(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("history_rpc_error:http_"):
        return text.split(":", 1)[1][:32]
    if text.startswith("history_rpc_error:"):
        return text.split(":", 1)[1][:32]
    return type(exc).__name__.lower()[:32] or "unknown"


def _filter_shapes(owner: str, lower: int, upper: int) -> list[tuple[str, dict[str, Any]]]:
    owner_topic = _topic_address(owner)
    modules = sorted(V2_MODULES)
    tokens = sorted(V2_COLLATERAL_TOKENS)
    event_topics = [POSITION_REDEEMED_TOPIC, POSITIONS_MERGED_TOPIC]
    rows: list[tuple[str, dict[str, Any]]] = [
        (
            "event_or_address_or",
            {"fromBlock": hex(lower), "toBlock": hex(upper), "address": modules,
             "topics": [event_topics, None, None, owner_topic]},
        ),
        (
            "event_scalar_address_or_topic",
            {"fromBlock": hex(lower), "toBlock": hex(upper), "address": modules[0],
             "topics": [event_topics, None, None, owner_topic]},
        ),
        (
            "event_scalar_address_scalar_topic",
            {"fromBlock": hex(lower), "toBlock": hex(upper), "address": modules[0],
             "topics": [POSITION_REDEEMED_TOPIC, None, None, owner_topic]},
        ),
        (
            "transfer_or_address",
            {"fromBlock": hex(lower), "toBlock": hex(upper), "address": tokens,
             "topics": [TRANSFER_TOPIC, None, owner_topic]},
        ),
        (
            "transfer_scalar_address",
            {"fromBlock": hex(lower), "toBlock": hex(upper), "address": tokens[0],
             "topics": [TRANSFER_TOPIC, None, owner_topic]},
        ),
    ]
    return rows


def _probe_endpoint(
    endpoint: str,
    accounts: list[dict[str, Any]],
    *,
    block_span: int,
    rpc_call: Callable[[str, str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    endpoint_kind = hashlib.sha256(endpoint.encode()).hexdigest()

    def call(method: str, params: list[Any]) -> Any:
        return rpc_call(endpoint, method, params) if rpc_call else _rpc_call(endpoint, method, params)

    try:
        latest = _hex_int(call("eth_blockNumber", []))
    except Exception as exc:
        return {"endpoint_sha256": endpoint_kind, "block_number": None, "error": _classify(exc), "accounts": []}
    if latest is None:
        return {"endpoint_sha256": endpoint_kind, "block_number": None, "error": "latest_block_unavailable", "accounts": []}
    lower = max(0, latest - block_span + 1)
    account_reports = []
    for account in accounts:
        owner = _lower_address(account.get("owner"))
        if not owner:
            continue
        categories: dict[str, int] = {}
        shape_reports = []
        for shape, log_filter in _filter_shapes(owner, lower, latest):
            try:
                value = call("eth_getLogs", [log_filter])
                rows = value if isinstance(value, list) else []
                category = "ok"
                row_count = len(rows)
            except Exception as exc:
                category = _classify(exc)
                row_count = 0
            categories[category] = categories.get(category, 0) + 1
            shape_reports.append({"shape": shape, "category": category, "row_count": row_count})
        account_reports.append({
            "owner_address_sha256": _owner_digest(owner),
            "shape_reports": shape_reports,
            "categories": categories,
        })
    return {
        "endpoint_sha256": endpoint_kind,
        "block_number": latest,
        "block_span": block_span,
        "accounts": account_reports,
    }


def probe(input_path: Path, endpoints: list[str], output: Path, *, block_span: int = DEFAULT_BLOCK_SPAN) -> dict[str, Any]:
    if not endpoints:
        raise ValueError("at least one endpoint is required")
    if not 1 <= int(block_span) <= MAX_BLOCK_SPAN:
        raise ValueError("block_span_out_of_bounds")
    document = json.loads(input_path.read_text(encoding="utf-8"))
    accounts = [row for row in document.get("accounts") or [] if isinstance(row, dict)]
    reports = [_probe_endpoint(endpoint.strip(), accounts, block_span=block_span) for endpoint in endpoints if endpoint.strip()]
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
        "evidence_boundary": "tiny read-only RPC compatibility probe; response categories do not establish settlement or absence",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": FORMAT, "endpoint_count": len(reports), "accounts": len(accounts)}, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-urls", required=True, help="Comma-separated read-only JSON-RPC endpoints")
    parser.add_argument("--block-span", type=int, default=DEFAULT_BLOCK_SPAN)
    args = parser.parse_args()
    probe(args.input, [value for value in args.rpc_urls.split(",")], args.output, block_span=args.block_span)


if __name__ == "__main__":
    main()
