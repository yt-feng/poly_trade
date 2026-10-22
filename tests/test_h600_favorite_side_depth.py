from types import SimpleNamespace

import pandas as pd

from analysis.h600_favorite_side_depth import FAVORITE_PRICE_FLOOR, THRESHOLDS, favorite_side_only


def test_favorite_side_filter_keeps_only_asks_at_or_above_half():
    frame = pd.DataFrame(
        [
            {"slug": "a", "price": 0.50},
            {"slug": "b", "price": 0.49},
            {"slug": "c", "price": "0.75"},
            {"slug": "d", "price": None},
        ]
    )
    result = favorite_side_only(frame)
    assert result["slug"].tolist() == ["a", "c"]
    assert FAVORITE_PRICE_FLOOR == 0.50


def test_h600_freezes_the_h578_depth_map():
    assert THRESHOLDS == (3.0, 5.0, 7.0)


def test_empty_candidate_filter_is_copy():
    frame = pd.DataFrame(columns=["price"])
    result = favorite_side_only(frame)
    assert result.empty
    assert result is not frame
