import math

import pandas as pd

from analysis.h156_h42_h43_target_admissibility import (
    CANDIDATES,
    ENTRY_QUOTE_CAP,
    TARGET_MULTIPLIER,
    chronological_blocks,
    minimum_win_quote_cap,
)


def test_target_cap_includes_maximum_slippage():
    assert math.isclose(TARGET_MULTIPLIER, 100 ** (1 / 10), rel_tol=1e-12)
    assert ENTRY_QUOTE_CAP < minimum_win_quote_cap(maximum_slippage=0.0)
    assert 0.43 < ENTRY_QUOTE_CAP < 0.45


def test_holdout_blocks_are_disjoint_and_complete():
    runs = [f"r{i}" for i in range(10)]
    blocks = chronological_blocks(runs, (4, 3, 3))
    assert [item for block in blocks for item in block] == runs
    assert len({item for block in blocks for item in block}) == len(runs)


def test_candidate_names_are_the_registered_causal_models():
    assert CANDIDATES == ("h42_causal_calibrated_edge", "h43_betabinomial_residual")
