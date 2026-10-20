import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import h413_strict_quality_h30_rolling_stability as h413


def test_h413_protocol_is_one_ledger_per_window_without_concatenation():
    assert h413.AUDIT_CANDIDATE == "h30_move_uncertainty"
    assert h413.REQUIRED_FILLS == 10
    assert h413.WINDOW_RUNS == 12
    assert h413.WINDOW_STRIDE == 4


def test_h413_cell_summary_counts_only_growth_positive_windows():
    rows = [
        {"slippage": 0.005, "depth_floor": 100.0, "first_ten_growth_positive": True,
         "first_ten_ending_cash_usd": 12.0, "first_ten_fills": 10},
        {"slippage": 0.005, "depth_floor": 100.0, "first_ten_growth_positive": False,
         "first_ten_ending_cash_usd": 4.0, "first_ten_fills": 1},
    ]
    result = h413.cell_summary(rows, 0.005, 100.0)
    assert result["window_count"] == 2
    assert result["growth_positive_windows"] == 1
    assert result["growth_window_fraction"] == 0.5
    assert result["minimum_first_ten_fills"] == 1
