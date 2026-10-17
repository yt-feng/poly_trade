from __future__ import annotations

import pandas as pd
import pytest
from analysis.causal_quantml_small_cash import STRATEGY, _blocks, add_candidate, apply_thresholds


def test_factor_and_tail_side_are_fixed_and_causal() -> None:
    frame = pd.DataFrame(
        {
            "mid_up_prob_2m": [0.50, 0.75, 0.25, 0.50],
            "size_imbalance_updown_2m": [0.8, 0.8, -0.8, 0.0],
        }
    )
    enriched = add_candidate(frame)
    assert enriched["quantml_size_imbalance_uncertainty"].round(6).tolist() == [0.8, 0.4, -0.4, 0.0]
    gated = apply_thresholds(enriched, low=-0.4, high=0.4)
    assert gated[f"{STRATEGY}_side"].tolist() == ["buy_up", "buy_up", "buy_down", "skip"]
    assert gated[f"{STRATEGY}_minute"].tolist() == [2, 2, 2, -1]


def test_middle_factor_is_skipped() -> None:
    frame = add_candidate(
        pd.DataFrame(
            {"mid_up_prob_2m": [0.5], "size_imbalance_updown_2m": [0.1]}
        )
    )
    gated = apply_thresholds(frame, low=-0.4, high=0.4)
    assert gated.loc[0, f"{STRATEGY}_side"] == "skip"
    assert gated.loc[0, f"{STRATEGY}_minute"] == -1


def test_stress_blocks_cover_holdout_only() -> None:
    runs = [f"run-{i}" for i in range(59)]
    blocks = _blocks(runs)
    assert [len(block) for block in blocks] == [12, 12, 12, 12, 11]
    assert [item for block in blocks for item in block] == runs


def test_stress_blocks_reject_selection_overlap() -> None:
    with pytest.raises(ValueError, match="BLOCK_INPUT_MUST_BE_HOLDOUT59"):
        _blocks([f"run-{i}" for i in range(79)])

