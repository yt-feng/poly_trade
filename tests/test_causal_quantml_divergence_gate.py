from __future__ import annotations

import pandas as pd

from analysis.causal_quantml_divergence_gate import STRATEGY, add_candidate, apply_thresholds


def test_divergence_gate_uses_btc_direction_and_filters_agreement() -> None:
    frame = pd.DataFrame(
        {
            "btc_move_2m": [10.0, -10.0, 10.0, 0.5],
            "mid_up_prob_change_2m": [-0.01, 0.01, 0.01, -0.01],
            "mid_up_prob_2m": [0.5, 0.5, 0.5, 0.5],
        }
    )
    out = add_candidate(frame)
    assert out[f"{STRATEGY}_consensus"].tolist() == [True, True, False, False]
    assert out.loc[0, STRATEGY] > 0
    assert out.loc[1, STRATEGY] < 0


def test_thresholds_do_not_admit_non_consensus_rows() -> None:
    frame = pd.DataFrame({STRATEGY: [0.3, -0.3, 0.1], f"{STRATEGY}_consensus": [True, True, False]})
    out = apply_thresholds(frame, -0.2, 0.2)
    assert out[f"{STRATEGY}_side"].tolist() == ["buy_up", "buy_down", "skip"]
    assert out[f"{STRATEGY}_minute"].tolist() == [2, 2, -1]
