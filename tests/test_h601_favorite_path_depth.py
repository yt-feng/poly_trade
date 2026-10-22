import pandas as pd

from analysis.h601_favorite_path_depth import (
    FAVORITE_PRICE_FLOOR,
    PATH_EFFICIENCY_FLOOR,
    THRESHOLDS,
    favorite_path_only,
)


def test_favorite_path_filter_requires_both_entry_visible_gates():
    frame = pd.DataFrame(
        {
            "price": [0.50, 0.49, 0.60, 0.60],
            "path_efficiency_first2m": [0.50, 0.90, 0.49, 0.80],
        }
    )
    out = favorite_path_only(frame)
    assert out.index.tolist() == [0, 3]


def test_h601_thresholds_are_frozen():
    assert FAVORITE_PRICE_FLOOR == 0.50
    assert PATH_EFFICIENCY_FLOOR == 0.50
    assert THRESHOLDS == (3.0, 5.0, 7.0)


def test_empty_filter_returns_independent_copy():
    frame = pd.DataFrame(columns=["price", "path_efficiency_first2m"])
    out = favorite_path_only(frame)
    assert out.empty
    assert out is not frame
