from analysis.h270_single_position_cash_turnover import DEPTH_FLOORS, SLIPPAGES, _group_passes


def _cell(fills=10, passed=True):
    return {"first_ten_fills": fills, "first_ten_passed": passed}


def test_h270_requires_all_declared_selection_cells():
    rows = [_cell() for _ in range(len(SLIPPAGES) * len(DEPTH_FLOORS))]
    assert _group_passes(rows)
    assert not _group_passes(rows[:-1])
    rows[-1] = _cell(fills=9)
    assert not _group_passes(rows)

