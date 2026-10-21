"""Reconcile the current paper frontier with the authenticated Canary gate.

H492 consumes an already materialized, redacted episode checkpoint.  It never
fetches data or account state.  Its purpose is to prevent the older readiness
helper (which only knew H30/H38) from being mistaken for the current H453
paper frontier and to keep paper stress results separate from receipt evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field}_required")
    return value


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field}_must_be_bool")
    return value


def build_report(state: dict[str, Any]) -> dict[str, Any]:
    h453 = _dict(state.get("h453_upper_cash_fraction_result"), "h453_upper_cash_fraction_result")
    h453_prereg = state.get("h453_preregistration", {})
    if not isinstance(h453_prereg, dict):
        raise ValueError("h453_preregistration_must_be_object")
    h465 = _dict(state.get("h465_corrected_result"), "h465_corrected_result")
    h480 = _dict(state.get("h480_observed_18h_release_clock"), "h480_observed_18h_release_clock")
    receipt = _dict(state.get("receipt_boundary_status"), "receipt_boundary_status")

    cells = h453.get("cells")
    if not isinstance(cells, list) or len(cells) != 4:
        raise ValueError("h453_four_execution_cells_required")
    h453_cells_pass = all(cell.get("all_windows_passed") is True for cell in cells if isinstance(cell, dict))
    if not h453_cells_pass:
        raise ValueError("h453_cell_shape_or_gate_invalid")
    if h453.get("actual_fills") != 0 or h453.get("verified_canary_fills") != 0 or h453.get("same_ledger_compounding_rows") != 0:
        raise ValueError("h453_must_remain_paper_only")

    h465_cells = h465.get("cells")
    if not isinstance(h465_cells, list) or not h465_cells:
        raise ValueError("h465_stress_cells_required")
    h465_all_pass = all(cell.get("all_windows_passed") is True for cell in h465_cells if isinstance(cell, dict))
    h480_pass = h480.get("slippage_0.005", {}).get("all_windows_passed") is True and h480.get("slippage_0.010", {}).get("all_windows_passed") is True

    owner_bundle = receipt.get("authenticated_owner_scoped_bundle_available") is True
    btc_bundle = receipt.get("btc_five_minute_bundle_available") is True
    actual_fills = state.get("actual_fills")
    verified_fills = state.get("verified_canary_fills")
    ledger_rows = state.get("same_ledger_compounding_rows")
    if any(type(value) is not int for value in (actual_fills, verified_fills, ledger_rows)):
        raise ValueError("current_fill_counts_must_be_integers")

    hard_blockers = []
    if not owner_bundle or not btc_bundle:
        hard_blockers.append("authenticated_owner_scoped_btc_five_minute_bundle_missing")
    if actual_fills < 10 or verified_fills < 10 or ledger_rows != 10:
        hard_blockers.append("ten_authenticated_same_ledger_rows_missing")
    if not h465_all_pass:
        hard_blockers.append("late_depth_cutoff_stress_not_stable")
    if not h480_pass:
        hard_blockers.append("long_cash_release_stress_not_stable")
    if state.get("live_orders_enabled") is True:
        hard_blockers.append("live_orders_must_remain_disabled_during_paper_reconciliation")

    return {
        "format": "h492-current-frontier-reconciliation-v1",
        "source_scope": "redacted_existing_episode_checkpoint_only",
        "paper_reference": {
            "hypothesis_id": "H453_H452_UPPER_CASH_FRACTION_CEILING",
            "candidate": "H447_H434_CAUSAL_BREAKEVEN_EXIT",
            "all_four_cells_passed": h453_cells_pass,
            "window_count": h453.get("window_count") or h453_prereg.get("window_count") or max(
                (cell.get("window_count", 0) for cell in cells if isinstance(cell, dict)),
                default=0,
            ),
            "minimum_first_ten_cash_usd": {
                "0.95@0.005": h453.get("cells")[0].get("minimum_first_ten_cash_usd"),
                "0.95@0.01": h453.get("cells")[1].get("minimum_first_ten_cash_usd"),
                "1.0@0.005": h453.get("cells")[2].get("minimum_first_ten_cash_usd"),
                "1.0@0.01": h453.get("cells")[3].get("minimum_first_ten_cash_usd"),
            },
            "paper_only": True,
        },
        "stress_reconciliation": {
            "h465_60_second_cutoff_all_windows_passed": h465_all_pass,
            "h465_passed_windows": [cell.get("passed_windows") for cell in h465_cells],
            "h480_18_hour_release_all_windows_passed": h480_pass,
        },
        "authenticated_boundary": {
            "owner_scoped_bundle": owner_bundle,
            "btc_five_minute_bundle": btc_bundle,
            "actual_fills": actual_fills,
            "verified_canary_fills": verified_fills,
            "same_ledger_compounding_rows": ledger_rows,
            "canary_ready": False,
        },
        "hard_blockers": hard_blockers,
        "decision": "keep_h453_frozen_as_paper_reference_and_wait_for_owner_scoped_bundle",
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "root_cause": "The current paper frontier and the authenticated evidence boundary are separate. H453 is stable only under the full late-depth paper protocol; H465 shows the edge is execution-time sensitive, while no owner-scoped bundle exists to count a Canary.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.state.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("state_object_required")
    report = build_report(document)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": report["format"], "decision": report["decision"], "canary_ready": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

