from __future__ import annotations

import pandas as pd

from analysis.h201_maker_touch0_h38_compound import _touch_at_or_below, add_frozen_h38_signal


def test_touch_proxy_includes_same_tick_and_rejects_worse_ask() -> None:
    assert _touch_at_or_below(pd.Series([25.0, 24.0]), 25.0) is True
    assert _touch_at_or_below(pd.Series([26.0, 25.01]), 25.0) is False


def test_touch_proxy_ignores_missing_values() -> None:
    assert _touch_at_or_below(pd.Series([float("nan")]), 25.0) is False


def test_h38_signal_keeps_frozen_path_gate_and_direction() -> None:
    frame = pd.DataFrame(
        {
            "btc_move_2m": [100.0, -100.0, 100.0, 50.0],
            "mid_up_prob_2m": [0.5, 0.5, 0.5, 0.9],
            "path_efficiency_first2m": [0.3, 0.3, 0.7, 0.3],
        }
    )
    out = add_frozen_h38_signal(frame)
    assert out["h201_maker_touch0_h38_side"].tolist() == ["buy_up", "buy_down", "skip", "skip"]
    assert out["h201_maker_touch0_h38_minute"].tolist() == [2, 2, 2, 2]
