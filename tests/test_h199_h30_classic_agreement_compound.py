import pandas as pd

from analysis.h199_h30_classic_agreement_compound import (
    AGREEMENT_CANDIDATE,
    add_agreement_candidate,
)


def test_agreement_keeps_only_same_direction_and_h30_execution_fields():
    frame = pd.DataFrame(
        {
            "h30_move_uncertainty_side": ["buy_up", "buy_down", "buy_up", "skip", None],
            "classic_breakout_up_side": ["buy_up", "buy_up", "skip", "skip", "buy_down"],
            "h30_move_uncertainty_minute": [2, 2, 2, 2, 2],
            "h30_move_uncertainty_fraction": [0.04, 0.04, 0.04, 0.04, 0.04],
        }
    )

    out = add_agreement_candidate(frame)
    assert out[f"{AGREEMENT_CANDIDATE}_side"].tolist() == ["buy_up", "skip", "skip", "skip", "skip"]
    assert out[f"{AGREEMENT_CANDIDATE}_minute"].tolist() == [2, 2, 2, 2, 2]
    assert out[f"{AGREEMENT_CANDIDATE}_fraction"].tolist() == [0.04] * 5
    assert set(out[f"{AGREEMENT_CANDIDATE}_component"]) == {"frozen_h30_classic_direction_agreement"}


def test_agreement_does_not_mutate_input():
    frame = pd.DataFrame(
        {
            "h30_move_uncertainty_side": ["buy_down"],
            "classic_breakout_up_side": ["buy_down"],
            "h30_move_uncertainty_minute": [2],
            "h30_move_uncertainty_fraction": [0.04],
        }
    )
    original = frame.copy(deep=True)
    add_agreement_candidate(frame)
    pd.testing.assert_frame_equal(frame, original)
