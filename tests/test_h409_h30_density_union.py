import pandas as pd

from analysis.h409_h30_density_union import CANDIDATE, add_candidate


def base_row(**changes):
    row = {
        "btc_move_2m": "0.1",
        "mid_up_prob_2m": "0.70",
        "path_efficiency_first2m": "0.60",
        "h30_move_uncertainty_side": "skip",
        "v1_adaptive_mix_side": "buy_down",
        "v1_adaptive_mix_minute": 2,
        "v1_adaptive_mix_fraction": 0.08,
    }
    row.update(changes)
    return pd.DataFrame([row])


def test_path_fallback_is_used_only_when_h30_skips():
    frame, diagnostics = add_candidate(base_row())
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_down"
    assert frame.loc[0, f"{CANDIDATE}_component"] == "v1_path_fallback"
    assert bool(diagnostics.loc[0, "fallback_eligible"])


def test_h30_has_precedence_over_fallback():
    frame, diagnostics = add_candidate(
        base_row(
            btc_move_2m="25",
            h30_move_uncertainty_minute=2,
            h30_move_uncertainty_fraction=0.04,
        )
    )
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_up"
    assert frame.loc[0, f"{CANDIDATE}_component"] == "h30_primary"
    assert not bool(diagnostics.loc[0, "fallback_eligible"])


def test_missing_path_fails_closed():
    frame, diagnostics = add_candidate(base_row(path_efficiency_first2m=None))
    assert frame.loc[0, f"{CANDIDATE}_side"] == "skip"
    assert frame.loc[0, f"{CANDIDATE}_component"] == "skip"
    assert not bool(diagnostics.loc[0, "fallback_eligible"])
