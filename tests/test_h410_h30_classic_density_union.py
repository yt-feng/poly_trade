import pandas as pd

from analysis.h410_h30_classic_density_union import CANDIDATE, add_candidate


def base_row(**changes):
    row = {
        "btc_move_2m": "0.1",
        "mid_up_prob_2m": "0.70",
        "path_efficiency_first2m": "0.60",
        "h30_move_uncertainty_side": "skip",
        "h30_move_uncertainty_minute": 2,
        "h30_move_uncertainty_fraction": 0.04,
        "btc_move_4m": 40,
        "buy_up_price_4m": 0.60,
        "gate_up": True,
    }
    row.update(changes)
    return pd.DataFrame([row])


def test_classic_fallback_is_used_only_when_h30_skips():
    frame, diagnostics = add_candidate(base_row())
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_up"
    assert frame.loc[0, f"{CANDIDATE}_minute"] == 4
    assert frame.loc[0, f"{CANDIDATE}_component"] == "classic_fallback"
    assert bool(diagnostics.loc[0, "fallback_eligible"])


def test_h30_has_precedence_over_classic_fallback():
    frame, diagnostics = add_candidate(
        base_row(
            btc_move_2m="25",
            h30_move_uncertainty_minute=2,
            h30_move_uncertainty_fraction=0.04,
        )
    )
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_up"
    assert frame.loc[0, f"{CANDIDATE}_minute"] == 2
    assert frame.loc[0, f"{CANDIDATE}_component"] == "h30_primary"
    assert not bool(diagnostics.loc[0, "fallback_eligible"])


def test_classic_missing_gate_fails_closed():
    frame, diagnostics = add_candidate(base_row(gate_up=False))
    assert frame.loc[0, f"{CANDIDATE}_side"] == "skip"
    assert frame.loc[0, f"{CANDIDATE}_component"] == "skip"
    assert not bool(diagnostics.loc[0, "fallback_eligible"])
