import pandas as pd

from analysis.h200_lag2_sign_reversal_compound import (
    REVERSAL_CANDIDATE,
    add_sign_reversal_candidate,
)


def test_sign_reversal_is_causal_and_parameter_free():
    frame = pd.DataFrame({"btc_move_2m": [1.2, -0.1, 0.0, None]})
    out = add_sign_reversal_candidate(frame)
    assert out[f"{REVERSAL_CANDIDATE}_side"].tolist() == ["buy_down", "buy_up", "skip", "skip"]
    assert out[f"{REVERSAL_CANDIDATE}_minute"].tolist() == [2, 2, 2, 2]
    assert out[f"{REVERSAL_CANDIDATE}_fraction"].tolist() == [0.04] * 4
    assert set(out[f"{REVERSAL_CANDIDATE}_component"]) == {"paper_derived_lag_sign_reversal"}


def test_sign_reversal_does_not_mutate_input():
    frame = pd.DataFrame({"btc_move_2m": [1.0]})
    original = frame.copy(deep=True)
    add_sign_reversal_candidate(frame)
    pd.testing.assert_frame_equal(frame, original)
