import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import h198_rolling_window_compound_stability as h198


def test_rolling_windows_overlap_without_reordering_or_concatenation():
    windows = h198.rolling_windows(["r1", "r2", "r3", "r4"], window_runs=3)
    assert [window["runs"] for window in windows] == [["r1", "r2", "r3"], ["r2", "r3", "r4"]]
    assert windows[0]["start_run"] == "r1"
    assert windows[1]["end_run"] == "r4"


def test_rolling_windows_reject_invalid_size():
    try:
        h198.rolling_windows(["r1", "r2"], window_runs=3)
    except ValueError as exc:
        assert str(exc) == "INSUFFICIENT_RUNS_FOR_WINDOW"
    else:
        raise AssertionError("expected insufficient-run failure")
