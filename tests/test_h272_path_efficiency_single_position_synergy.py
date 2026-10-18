from analysis.h272_path_efficiency_single_position_synergy import CANDIDATES, DEPTH_FLOORS, SLIPPAGES, _group_passes


def test_h272_requires_all_registered_execution_cells():
    rows = [{"first_ten_fills": 10, "first_ten_passed": True} for _ in range(len(SLIPPAGES) * len(DEPTH_FLOORS))]
    assert _group_passes(rows)
    assert len(CANDIDATES) == 2
    assert not _group_passes(rows[:-1])

