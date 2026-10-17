from __future__ import annotations

import pytest

from analysis.causal_history_cooldown_cash import BLOCK_SIZES, holdout_blocks


def test_holdout_blocks_cover_only_59_runs() -> None:
    runs = [f"run-{i}" for i in range(sum(BLOCK_SIZES))]
    blocks = holdout_blocks(runs)
    assert [len(block) for block in blocks] == list(BLOCK_SIZES)
    assert [item for block in blocks for item in block] == runs


def test_selection_overlap_is_rejected() -> None:
    with pytest.raises(ValueError, match="BLOCK_INPUT_MUST_BE_HOLDOUT59"):
        holdout_blocks([f"run-{i}" for i in range(79)])
