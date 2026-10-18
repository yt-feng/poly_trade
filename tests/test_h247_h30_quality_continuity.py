import pandas as pd

from analysis.h247_h30_quality_continuity import (
    CANDIDATE,
    STRATEGY,
    apply_quality_gate,
    quote_count_floor,
)


def frame():
    return pd.DataFrame(
        {
            "run_name": ["r1", "r1", "r2"],
            f"{CANDIDATE}_side": ["buy_up", "buy_down", "skip"],
            f"{CANDIDATE}_minute": [2, 2, 2],
            f"{CANDIDATE}_fraction": [0.04, 0.04, 0.0],
            "quote_count_first2m": [10, 20, 99],
            "spread_up_median_first2m": [0.03, 0.20, 0.01],
            "spread_down_median_first2m": [0.20, 0.03, 0.01],
            "overround_median_first2m": [0.02, 0.02, 0.02],
            "buy_up_size_2m": [160, 10, 200],
            "buy_down_size_2m": [10, 160, 200],
            "bid_depth_imbalance_updown_2m": [0.20, -0.20, 0.0],
        }
    )


def test_quote_count_floor_uses_only_signal_rows_in_selection():
    assert quote_count_floor(frame(), ["r1"]) == 15.0


def test_gate_is_side_specific_and_preserves_frozen_signal_direction():
    out = apply_quality_gate(frame(), 15.0)
    assert out[f"{STRATEGY}_side"].tolist() == ["skip", "buy_down", "skip"]
    assert out[f"{STRATEGY}_fraction"].tolist() == [0.0, 0.04, 0.0]


def test_quality_gate_never_uses_outcome_or_future_fields():
    source = frame()
    gated = apply_quality_gate(source, 15.0)
    source["outcome_up"] = [0, 1, 0]
    source["settlement_payout"] = [100, 0, 100]
    assert gated[f"{STRATEGY}_side"].tolist() == ["skip", "buy_down", "skip"]
