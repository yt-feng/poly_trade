from analysis.h158_registry_exact_variable_multiplier_target_chain import CANDIDATES, chronological_blocks, configs_pass


def test_candidate_family_is_frozen():
    assert CANDIDATES == ("classic_breakout_up", "micro_book_consensus_up")


def test_blocks_cover_without_overlap():
    runs = [f"r{i}" for i in range(10)]
    blocks = chronological_blocks(runs, (4, 3, 3))
    assert [item for block in blocks for item in block] == runs
    assert len({item for block in blocks for item in block}) == len(runs)


def test_target_selection_requires_all_cost_cells_and_allows_variable_multipliers():
    rows = []
    for slippage in (0.005, 0.01):
        for depth in (100.0, 150.0):
            rows.append({"first_ten_passed": True, "first_ten_fills": 10, "first_ten_target_growth_passed": True})
    assert configs_pass(rows, target=True)
    rows[-1]["first_ten_target_growth_passed"] = False
    assert not configs_pass(rows, target=True)
