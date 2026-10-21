import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h492_current_frontier_reconciliation import build_report


def _state() -> dict[str, object]:
    cells = [
        {"all_windows_passed": True, "window_count": 17, "minimum_first_ten_cash_usd": value}
        for value in (12.429613141240278, 11.86489657664916, 12.526638945802254, 11.934788232313801)
    ]
    return {
        "h453_preregistration": {"window_count": 17},
        "h453_upper_cash_fraction_result": {
            "cells": cells,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
        },
        "h465_corrected_result": {
            "cells": [
                {"all_windows_passed": False, "passed_windows": 15},
                {"all_windows_passed": False, "passed_windows": 14},
            ]
        },
        "h480_observed_18h_release_clock": {
            "slippage_0.005": {"all_windows_passed": True},
            "slippage_0.010": {"all_windows_passed": True},
        },
        "receipt_boundary_status": {
            "authenticated_owner_scoped_bundle_available": False,
            "btc_five_minute_bundle_available": False,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def test_current_frontier_keeps_paper_and_authenticated_gates_separate():
    state = _state()
    report = build_report(state)
    assert report["paper_reference"]["all_four_cells_passed"] is True
    assert report["paper_reference"]["paper_only"] is True
    assert report["stress_reconciliation"]["h465_60_second_cutoff_all_windows_passed"] is False
    assert report["stress_reconciliation"]["h480_18_hour_release_all_windows_passed"] is True
    assert report["authenticated_boundary"]["actual_fills"] == 0
    assert report["authenticated_boundary"]["verified_canary_fills"] == 0
    assert report["authenticated_boundary"]["canary_ready"] is False
    assert "authenticated_owner_scoped_btc_five_minute_bundle_missing" in report["hard_blockers"]


def test_h453_cannot_be_relabelled_as_canary_by_increasing_paper_counts():
    state = _state()
    altered = copy.deepcopy(state)
    altered["h453_upper_cash_fraction_result"]["actual_fills"] = 10
    try:
        build_report(altered)
    except ValueError as exc:
        assert str(exc) == "h453_must_remain_paper_only"
    else:
        raise AssertionError("paper artifact was relabelled as authenticated")

