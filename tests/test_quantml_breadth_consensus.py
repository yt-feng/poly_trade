import numpy as np
import pandas as pd

from analysis.causal_quantml_breadth_consensus import STRATEGY, add_candidate, apply_thresholds


def _frame(**overrides):
    base = {
        "size_imbalance_updown_2m": [0.30, -0.30, 0.30],
        "sell_size_imbalance_updown_2m": [0.20, -0.20, -0.20],
        "btc_move_1m": [10.0, -10.0, 10.0],
        "btc_move_2m": [20.0, -20.0, -20.0],
        "mid_up_prob_change_2m": [0.01, -0.01, 0.01],
        "mid_up_prob_2m": [0.50, 0.50, 0.50],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_conflicting_horizons_are_vetoed():
    result = add_candidate(_frame())
    assert bool(result.loc[0, f"{STRATEGY}_consensus"])
    assert bool(result.loc[1, f"{STRATEGY}_consensus"])
    assert not bool(result.loc[2, f"{STRATEGY}_consensus"])
    assert np.isfinite(result.loc[0, STRATEGY])
    assert np.isfinite(result.loc[1, STRATEGY])
    assert np.isnan(result.loc[2, STRATEGY])


def test_thresholds_never_admit_invalid_consensus():
    result = apply_thresholds(add_candidate(_frame()), low=-0.10, high=0.10)
    assert result.loc[0, f"{STRATEGY}_side"] == "buy_up"
    assert result.loc[1, f"{STRATEGY}_side"] == "buy_down"
    assert result.loc[2, f"{STRATEGY}_side"] == "skip"
    assert result.loc[0, f"{STRATEGY}_minute"] == 2
    assert result.loc[1, f"{STRATEGY}_minute"] == 2
    assert result.loc[2, f"{STRATEGY}_minute"] == -1
