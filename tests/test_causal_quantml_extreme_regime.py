from __future__ import annotations

import pandas as pd

from analysis.causal_quantml_extreme_regime import STRATEGY, add_candidate, apply_thresholds


def test_extreme_regime_uses_larger_side_and_liquidity_gate() -> None:
    frame = pd.DataFrame({
        "size_imbalance_updown_2m": [0.20, -0.20, 0.20],
        "sell_size_imbalance_updown_2m": [0.10, -0.10, 0.20],
        "spread_up_median_first2m": [0.04, 0.04, 0.08],
        "spread_down_median_first2m": [0.05, 0.05, 0.09],
        "overround_median_first2m": [0.03, 0.03, 0.03],
        "buy_up_size_2m": [100, 100, 100],
        "buy_down_size_2m": [90, 90, 90],
        "sell_up_size_2m": [80, 80, 80],
        "sell_down_size_2m": [80, 80, 80],
        "mid_up_prob_2m": [0.5, 0.5, 0.5],
    })
    out = add_candidate(frame)
    assert out[f"{STRATEGY}_regime"].tolist() == [True, True, False]
    assert out.loc[0, STRATEGY] > 0
    assert out.loc[1, STRATEGY] < 0


def test_thresholds_keep_non_regime_rows_skipped() -> None:
    frame = pd.DataFrame({STRATEGY: [0.30, -0.30, 0.20], f"{STRATEGY}_regime": [True, True, False]})
    out = apply_thresholds(frame, -0.25, 0.25)
    assert out[f"{STRATEGY}_side"].tolist() == ["buy_up", "buy_down", "skip"]
    assert out[f"{STRATEGY}_minute"].tolist() == [2, 2, -1]
