import pandas as pd

from analysis.h406_old_clean_h30_quality_prefix import chronological_blocks


def test_chronological_blocks_cover_holdout_without_concatenation():
    runs = [f"run-{i}" for i in range(59)]
    blocks = chronological_blocks(runs)
    assert [len(block) for block in blocks] == [12, 12, 12, 12, 11]
    assert [item for block in blocks for item in block] == runs


def test_block_builder_rejects_wrong_holdout_length():
    try:
        chronological_blocks(["only-one"])
    except ValueError as exc:
        assert str(exc) == "BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY"
    else:
        raise AssertionError("wrong holdout length must fail closed")
