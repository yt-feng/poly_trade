import pandas as pd

from analysis.h404_old_clean_portfolio_path_synergy import add_fixed_candidate


def test_path_filter_is_fixed_and_fail_closed_for_missing_efficiency():
    frame = pd.DataFrame(
        {
            "path_efficiency_first2m": [0.50, 0.499, None],
            "portfolio_conservative_v2_side": ["buy_up", "buy_down", "buy_up"],
            "portfolio_conservative_v2_minute": [2, 2, 2],
            "portfolio_conservative_v2_fraction": [0.04, 0.04, 0.04],
        }
    )
    out = add_fixed_candidate(frame)
    assert list(out["portfolio_conservative_v2_side"]) == ["skip", "skip", "skip"]


def test_candidate_preparation_does_not_mutate_input():
    frame = pd.DataFrame(
        {
            "path_efficiency_first2m": [0.9],
            "portfolio_conservative_v2_side": ["buy_up"],
            "portfolio_conservative_v2_minute": [2],
            "portfolio_conservative_v2_fraction": [0.04],
        }
    )
    original = frame.copy(deep=True)
    out = add_fixed_candidate(frame)
    pd.testing.assert_frame_equal(frame, original)
    assert list(out["portfolio_conservative_v2_side"]) == ["skip"]
