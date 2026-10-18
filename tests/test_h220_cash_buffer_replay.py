import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h220_cash_buffer_replay import BUFFER_LEVELS, CANDIDATES, select_candidate


def row(candidate, buffer, *, passed=True, ending=12.0, fills=10):
    return {
        "candidate": candidate,
        "minimum_free_cash_after_entry": buffer,
        "first_ten_passed": passed,
        "first_ten_fills": fills,
        "first_ten_ending_cash_usd": ending,
    }


def cells(candidate, buffer, ending):
    return [row(candidate, buffer, ending=ending) for _ in range(4)]


def test_selection_requires_all_cost_depth_cells_and_ranks_worst_endpoint():
    rows = cells(CANDIDATES[0], BUFFER_LEVELS[0], 15.0)
    rows += cells(CANDIDATES[0], BUFFER_LEVELS[1], 16.0)
    rows[-1]["first_ten_passed"] = False
    rows += cells(CANDIDATES[1], BUFFER_LEVELS[0], 14.0)
    choice = select_candidate(rows)
    assert choice["candidate"] == CANDIDATES[0]
    assert choice["minimum_free_cash_after_entry"] == BUFFER_LEVELS[0]


def test_selection_returns_none_when_no_buffer_group_survives():
    rows = cells(CANDIDATES[0], BUFFER_LEVELS[0], 15.0)
    rows[0]["first_ten_fills"] = 9
    assert select_candidate(rows) is None
