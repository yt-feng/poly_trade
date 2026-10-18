import pytest

from analysis.causal_path_efficiency_stress import (
    HOLDOUT_BLOCK_SIZES,
    chronological_blocks,
)


def test_stress_blocks_cover_holdout_only_and_preserve_order():
    holdout = [f"holdout-{index}" for index in range(sum(HOLDOUT_BLOCK_SIZES))]

    blocks = chronological_blocks(holdout, HOLDOUT_BLOCK_SIZES)

    assert [len(block) for block in blocks] == list(HOLDOUT_BLOCK_SIZES)
    assert [item for block in blocks for item in block] == holdout
    assert set().union(*map(set, blocks)) == set(holdout)


def test_stress_blocks_reject_selection_contamination():
    holdout = [f"holdout-{index}" for index in range(sum(HOLDOUT_BLOCK_SIZES) - 1)]

    with pytest.raises(ValueError, match="BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY"):
        chronological_blocks(holdout, HOLDOUT_BLOCK_SIZES)


def test_stress_blocks_reject_duplicate_run_ids():
    holdout = [f"holdout-{index}" for index in range(sum(HOLDOUT_BLOCK_SIZES))]
    holdout[-1] = holdout[-2]

    with pytest.raises(ValueError, match="BLOCKS_MUST_NOT_OVERLAP"):
        chronological_blocks(holdout, HOLDOUT_BLOCK_SIZES)
