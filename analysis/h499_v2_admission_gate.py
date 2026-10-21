"""Audit the final Canary gate after binding H496/H497 V2 path evidence.

This is a negative, read-only contract audit.  It supplies the current empty
state and proves that the gate remains closed without owner-scoped receipts;
it never reads a wallet, opens a socket, or creates an order.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .h330_receipt_gated_admission_audit import audit as h330_audit
except ImportError:  # pragma: no cover - direct script execution
    from h330_receipt_gated_admission_audit import audit as h330_audit


EMPTY_STATE: dict[str, Any] = {
    "actual_fills": 0,
    "verified_canary_fills": 0,
    "same_ledger_compounding_rows": 0,
    "owner_scoped_bundle_available": False,
    "owner_scoped_full_settlement_cash_bundle_available": False,
    "v2_path_bundle_validated": False,
    "balance_continuity_proven": False,
    "cash_release_receipt_rows": 0,
    "settlement_receipt_rows": 0,
}


def audit() -> dict[str, Any]:
    result = h330_audit(dict(EMPTY_STATE))
    checks = {
        "v2_path_is_hard_requirement": result["admission_contract"]["v2_path_bundle_validated"] is True,
        "empty_state_stays_closed": result["canary_admission_passed"] is False,
        "no_fills_are_admitted": result["actual_fills"] == 0 and result["verified_canary_fills"] == 0,
        "no_live_orders": result["live_orders_enabled"] is False,
        "no_new_data": result["new_data_collected"] is False,
    }
    return {
        "format": "h499-v2-admission-gate-audit-v1",
        "hypothesis_id": "H499_V2_PATH_BOUND_CANARY_ADMISSION_GATE",
        "h330_result": result,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "decision": "keep_gate_closed_until_owner_scoped_v2_cash_chain",
        "root_cause": "The prior admission contract did not require an official V2 path binding; H499 makes that evidence mandatory while preserving the zero-count boundary for the empty state.",
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    if not report["all_checks_passed"]:
        raise SystemExit("h499_v2_admission_gate_failed")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": report["format"], "all_checks_passed": True, "canary_admission_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
