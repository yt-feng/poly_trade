import pandas as pd

from analysis.h411_h30_milddrop_density_union import CANDIDATE, add_candidate


def base_row(**changes):
    row = {
        "btc_move_2m": -20,
        "mid_up_prob_2m": 0.99,
        "path_efficiency_first2m": 0.60,
        "size_imbalance_updown_2m": -0.10,
        "book_pressure_down_2m": 0.0,
        "h30_move_uncertainty_minute": 2,
        "h30_move_uncertainty_fraction": 0.04,
        "gate_down": True,
    }
    row.update(changes)
    return pd.DataFrame([row])


def test_milddrop_fallback_is_used_only_when_h30_skips():
    frame, diagnostics = add_candidate(base_row())
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_down"
    assert frame.loc[0, f"{CANDIDATE}_minute"] == 2
    assert frame.loc[0, f"{CANDIDATE}_component"] == "classic_fallback"
    assert bool(diagnostics.loc[0, "fallback_eligible"])


def test_h30_has_precedence_over_milddrop_fallback():
    frame, diagnostics = add_candidate(base_row(mid_up_prob_2m=0.50))
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_down"
    assert frame.loc[0, f"{CANDIDATE}_minute"] == 2
    assert frame.loc[0, f"{CANDIDATE}_component"] == "h30_primary"
    assert not bool(diagnostics.loc[0, "fallback_eligible"])


def test_milddrop_missing_gate_fails_closed():
    frame, diagnostics = add_candidate(base_row(gate_down=False))
    assert frame.loc[0, f"{CANDIDATE}_side"] == "skip"
    assert frame.loc[0, f"{CANDIDATE}_component"] == "skip"
    assert not bool(diagnostics.loc[0, "fallback_eligible"])
