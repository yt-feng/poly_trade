import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import h197_registry_prefix_survivor_holdout as h197


def row(candidate, *, passed=True, fills=10, endpoint=20.0):
    return {
        "candidate": candidate,
        "first_ten_growth_positive": passed,
        "first_ten_fills": fills,
        "first_ten_ending_cash_usd": endpoint,
    }


def test_selects_worst_cell_survivor_from_registered_candidates():
    rows = []
    for candidate, endpoint in [(h197.CANDIDATES[0], 20.0), (h197.CANDIDATES[1], 40.0)]:
        for offset in range(4):
            rows.append(row(candidate, endpoint=endpoint - offset))
    selected = h197.select_prefix_survivor(rows)
    assert selected is not None
    assert selected["candidate"] == h197.CANDIDATES[1]


def test_one_failed_cost_depth_cell_rejects_candidate():
    rows = [row(h197.CANDIDATES[0]) for _ in range(3)]
    rows.append(row(h197.CANDIDATES[0], passed=False, fills=9))
    assert h197.select_prefix_survivor(rows) is None

