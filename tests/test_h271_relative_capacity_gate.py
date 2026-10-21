from analysis.h271_relative_capacity_gate import (
    CAPACITY_RATIOS,
    DEPTH_FLOORS,
    HOLDOUT_BLOCK_SIZES,
    SLIPPAGES,
    _group_passes,
    _holdout_block_passes,
)


def _row(split: str, *, passed: bool = True) -> dict[str, object]:
    return {
        "split": split,
        "first_ten_fills": 10 if passed else 9,
        "first_ten_passed": passed,
    }


def test_pooled_group_requires_one_complete_cell_per_configuration() -> None:
    rows = [_row("holdout59") for _ in range(len(SLIPPAGES) * len(DEPTH_FLOORS) * len(CAPACITY_RATIOS))]
    assert _group_passes(rows)
    assert not _group_passes(rows[:-1])


def test_contiguous_holdout_gate_rejects_one_failed_block_even_when_pooled_passes() -> None:
    rows = []
    cells = len(SLIPPAGES) * len(DEPTH_FLOORS) * len(CAPACITY_RATIOS)
    for block_index in range(1, len(HOLDOUT_BLOCK_SIZES) + 1):
        rows.extend(_row(f"holdout_block_{block_index}", passed=block_index != 3) for _ in range(cells))
    assert not _holdout_block_passes(rows)


def test_contiguous_holdout_gate_requires_all_five_blocks() -> None:
    cells = len(SLIPPAGES) * len(DEPTH_FLOORS) * len(CAPACITY_RATIOS)
    rows = [_row(f"holdout_block_{block_index}") for block_index in range(1, len(HOLDOUT_BLOCK_SIZES) + 1) for _ in range(cells)]
    assert _holdout_block_passes(rows)
    assert not _holdout_block_passes(rows[:-1])
