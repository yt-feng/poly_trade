from __future__ import annotations

import pytest

from analysis.causal_path_efficiency_cash_replay import (
    HOLDOUT_BLOCK_SIZES,
    SELECTION_BLOCK_SIZES,
    chronological_blocks,
)


def test_selection_and_holdout_block_contracts_are_disjoint() -> None:
    selection = [f"selection-{i}" for i in range(sum(SELECTION_BLOCK_SIZES))]
    holdout = [f"holdout-{i}" for i in range(sum(HOLDOUT_BLOCK_SIZES))]
    selection_blocks = chronological_blocks(selection, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout, HOLDOUT_BLOCK_SIZES)
    assert [len(block) for block in selection_blocks] == list(SELECTION_BLOCK_SIZES)
    assert [len(block) for block in holdout_blocks] == list(HOLDOUT_BLOCK_SIZES)
    assert set().union(*selection_blocks).isdisjoint(set().union(*holdout_blocks))


def test_block_partition_rejects_selection_overlap_shape() -> None:
    with pytest.raises(ValueError, match="BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY"):
        chronological_blocks([f"run-{i}" for i in range(79)], HOLDOUT_BLOCK_SIZES)
