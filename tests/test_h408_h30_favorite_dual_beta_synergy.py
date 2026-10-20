import numpy as np
import pandas as pd

from analysis.h408_h30_favorite_dual_beta_synergy import CANDIDATE, add_candidate


def base_row(**changes):
    row = {
        "h30_move_uncertainty_side": "buy_up",
        "btc_move_2m": "0.1",
        "realized_vol_first2m": "0.02",
        "mid_up_prob_2m": "0.70",
        "buy_up_price_2m": "0.55",
        "buy_down_price_2m": "0.45",
    }
    row.update(changes)
    return pd.DataFrame([row])


def test_favorite_dual_beta_gate_admits_favorite_edge_inside_band():
    frame, diagnostics = add_candidate(base_row())
    assert frame.loc[0, f"{CANDIDATE}_side"] == "buy_up"
    assert frame.loc[0, f"{CANDIDATE}_fraction"] == 0.04
    assert bool(diagnostics.loc[0, "favorite"])
    assert bool(diagnostics.loc[0, "eligible"])


def test_underdog_side_is_rejected_even_when_model_edge_is_positive():
    frame, diagnostics = add_candidate(base_row(mid_up_prob_2m="0.30", buy_up_price_2m="0.45"))
    assert frame.loc[0, f"{CANDIDATE}_side"] == "skip"
    assert not bool(diagnostics.loc[0, "favorite"])


def test_edge_above_winner_curse_cap_is_rejected():
    frame, diagnostics = add_candidate(
        base_row(mid_up_prob_2m="0.70", buy_up_price_2m="0.50", btc_move_2m="1000")
    )
    assert frame.loc[0, f"{CANDIDATE}_side"] == "skip"
    assert diagnostics.loc[0, "edge_after_fee"] > 0.25


def test_missing_volatility_fails_closed():
    frame, diagnostics = add_candidate(base_row(realized_vol_first2m=np.nan))
    assert frame.loc[0, f"{CANDIDATE}_side"] == "skip"
    assert not bool(diagnostics.loc[0, "eligible"])
