"""H517: bounded RPC block-span matrix for the proven scalar log shape."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

try:
    from .h300_owner_scoped_payout_transfer_capture import DEFAULT_COLLATERAL_TOKENS, TRANSFER_TOPIC
    from .h301_owner_scoped_settlement_history_scan import _hex_int, _rpc_call
except ImportError:  # direct script execution
    from h300_owner_scoped_payout_transfer_capture import DEFAULT_COLLATERAL_TOKENS, TRANSFER_TOPIC
    from h301_owner_scoped_settlement_history_scan import _hex_int, _rpc_call


FORMAT = "h517-rpc-block-span-matrix-v1"
DEFAULT_SPANS = (20, 50, 100, 250, 500, 1000)
MAX_SPAN = 5000


def _classify(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("history_rpc_error:"):
        return text.split(":", 1)[1][:32]
    return type(exc).__name__.lower()[:32] or "unknown"


def _probe_endpoint(
    endpoint: str,
    spans: list[int],
    *,
    rpc_call: Callable[[str, str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    endpoint_sha256 = hashlib.sha256(endpoint.encode()).hexdigest()

    def call(method: str, params: list[Any]) -> Any:
        return rpc_call(endpoint, method, params) if rpc_call else _rpc_call(endpoint, method, params)

    try:
        latest = _hex_int(call("eth_blockNumber", []))
    except Exception as exc:
        return {"endpoint_sha256": endpoint_sha256, "block_number": None, "error": _classify(exc), "spans": []}
    if latest is None:
        return {"endpoint_sha256": endpoint_sha256, "block_number": None, "error": "latest_block_unavailable", "spans": []}
    token = sorted(DEFAULT_COLLATERAL_TOKENS)[0]
    results = []
    for span in spans:
        lower = max(0, latest - span + 1)
        query = {"fromBlock": hex(lower), "toBlock": hex(latest), "address": token, "topics": [TRANSFER_TOPIC]}
        try:
            value = call("eth_getLogs", [query])
            results.append({"span": span, "category": "ok", "row_count": len(value) if isinstance(value, list) else 0})
        except Exception as exc:
            results.append({"span": span, "category": _classify(exc), "row_count": 0})
    return {"endpoint_sha256": endpoint_sha256, "block_number": latest, "spans": results}


def probe(
    endpoints: list[str],
    spans: list[int],
    output: Path,
    *,
    rpc_call: Callable[[str, str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    cleaned_endpoints = [value.strip() for value in endpoints if value.strip()]
    if not cleaned_endpoints:
        raise ValueError("at_least_one_endpoint_required")
    normalized_spans = sorted(set(int(value) for value in spans))
    if not normalized_spans or any(value < 1 or value > MAX_SPAN for value in normalized_spans):
        raise ValueError("span_out_of_bounds")
    result = {
        "format": FORMAT,
        "endpoint_count": len(cleaned_endpoints),
        "spans": normalized_spans,
        "reports": [_probe_endpoint(endpoint, normalized_spans, rpc_call=rpc_call) for endpoint in cleaned_endpoints],
        "filter_shape": "scalar collateral token address plus TRANSFER_TOPIC",
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "read-only provider block-span compatibility probe; row counts do not establish owner settlement or absence",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": FORMAT, "endpoint_count": len(cleaned_endpoints), "spans": normalized_spans}, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-urls", required=True)
    parser.add_argument("--spans", default=",".join(str(value) for value in DEFAULT_SPANS))
    args = parser.parse_args()
    probe([value for value in args.rpc_urls.split(",")], [int(value) for value in args.spans.split(",")], args.output)


if __name__ == "__main__":
    main()
