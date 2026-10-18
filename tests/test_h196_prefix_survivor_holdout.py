import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h196_prefix_survivor_holdout import select_prefix_survivor


def row(strategy, candidate, fraction, *, passed=True, fills=10, endpoint=20.0):
    return {
        "strategy": strategy,
        "candidate": candidate,
        "fraction": fraction,
        "first_ten_passed": passed,
        "first_ten_fills": fills,
        "first_ten_ending_cash_usd": endpoint,
    }


def test_selects_highest_worst_cell_survivor_without_target_growth_gate():
    rows = []
    for strategy, candidate, fraction, endpoint in [
        ("classic_breakout_up__fraction_0p40", "classic_breakout_up", 0.4, 25.0),
        ("classic_breakout_up__fraction_0p80", "classic_breakout_up", 0.8, 70.0),
    ]:
        for i in range(4):
            rows.append(row(strategy, candidate, fraction, endpoint=endpoint - i))
    selected = select_prefix_survivor(rows)
    assert selected is not None
    assert selected["strategy"] == "classic_breakout_up__fraction_0p80"


def test_rejects_candidate_with_one_missing_ten_fill_cell():
    rows = [row("candidate__fraction_0p80", "candidate", 0.8) for _ in range(3)]
    rows.append(row("candidate__fraction_0p80", "candidate", 0.8, fills=9, passed=False))
    assert select_prefix_survivor(rows) is None
