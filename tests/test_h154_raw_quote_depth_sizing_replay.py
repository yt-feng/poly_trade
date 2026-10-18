from analysis.h154_raw_quote_depth_sizing_replay import (
    DEPTH_FLOORS,
    REQUIRED_FILLS,
    SIZING_FRACTIONS,
    _group_passes,
    _select,
)
from pathlib import Path


def test_sizing_grid_and_depth_stress_are_frozen():
    assert SIZING_FRACTIONS == (0.04, 0.08, 0.16, 0.32)
    assert DEPTH_FLOORS == (100.0, 150.0)
    assert REQUIRED_FILLS == 10


def test_selection_requires_every_declared_cost_and_depth_configuration():
    base = {
        "candidate": "h30_move_uncertainty",
        "fraction": 0.04,
        "first_ten_fills": 10,
        "first_ten_passed": True,
        "first_ten_ending_cash_usd": 12.0,
    }
    rows = [
        {**base, "slippage": slip, "depth_floor": depth}
        for slip in (0.005, 0.01)
        for depth in (100.0, 150.0)
    ]
    assert _group_passes(rows)
    rows[-1] = {**rows[-1], "first_ten_passed": False}
    assert not _group_passes(rows)


def test_selection_ranks_by_worst_first_ten_cash():
    rows = []
    for candidate, fraction, cash in [
        ("h30_move_uncertainty", 0.04, 12.0),
        ("h30_move_uncertainty", 0.08, 13.0),
    ]:
        for slip in (0.005, 0.01):
            for depth in (100.0, 150.0):
                rows.append({
                    "candidate": candidate,
                    "fraction": fraction,
                    "slippage": slip,
                    "depth_floor": depth,
                    "first_ten_fills": 10,
                    "first_ten_passed": True,
                    "first_ten_ending_cash_usd": cash,
                })
    choice = _select(rows)
    assert choice is not None
    assert choice["fraction"] == 0.08


def test_workflow_keeps_source_commit_available_to_seal_step():
    text = (Path(__file__).parents[1] / ".github" / "workflows" / "h154-raw-quote-depth-sizing-replay.yml").read_text()
    assert "    env:\n      SOURCE_DATA_COMMIT: bbda5f2050685f63153621a54641ebdfab943487" in text
    assert "os.environ['SOURCE_DATA_COMMIT']" in text
