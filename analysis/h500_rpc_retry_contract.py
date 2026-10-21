"""Offline contract audit for bounded read-only Polygon RPC retries."""
from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

try:
    from . import h301_owner_scoped_settlement_history_scan as scan
except ImportError:  # pragma: no cover - direct script execution
    import h301_owner_scoped_settlement_history_scan as scan


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'


def audit() -> dict:
    original_urlopen = scan.urlopen
    original_sleep = scan.time.sleep
    checks = {}
    try:
        transient_attempts = []

        def transient(request, timeout):
            transient_attempts.append(request.full_url)
            if len(transient_attempts) == 1:
                raise HTTPError(request.full_url, 503, "temporary", {}, io.BytesIO())
            return _Response()

        scan.urlopen = transient
        scan.time.sleep = lambda _seconds: None
        checks["transient_retry_then_success"] = (
            scan._rpc_call("https://rpc.example", "eth_blockNumber", []) == {"ok": True}
            and len(transient_attempts) == 2
        )

        permanent_attempts = []

        def permanent(request, timeout):
            permanent_attempts.append(request.full_url)
            raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())

        scan.urlopen = permanent
        try:
            scan._rpc_call("https://rpc.example", "eth_blockNumber", [])
        except RuntimeError as exc:
            checks["permanent_error_not_retried"] = str(exc) == "history_rpc_error:http_403" and len(permanent_attempts) == 1
        else:
            checks["permanent_error_not_retried"] = False
    finally:
        scan.urlopen = original_urlopen
        scan.time.sleep = original_sleep
    return {
        "format": "h500-rpc-retry-contract-audit-v1",
        "hypothesis_id": "H500_BOUNDED_READ_ONLY_RPC_RETRY",
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "decision": "retry_transient_receipt_provider_failures_without_changing_admission",
        "root_cause": "The prior receipt probe recorded provider failures without a bounded retry at the JSON-RPC transport boundary; permanent authorization errors must still fail immediately and remain redacted.",
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    if not report["all_checks_passed"]:
        raise SystemExit("h500_rpc_retry_contract_failed")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": report["format"], "all_checks_passed": True}, sort_keys=True))


if __name__ == "__main__":
    main()
