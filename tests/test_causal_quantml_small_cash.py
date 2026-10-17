from __future__ import annotations

import pandas as pd
from analysis.causal_quantml_small_cash import STRATEGY, add_candidate, apply_thresholds


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

