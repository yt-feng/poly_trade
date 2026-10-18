import numpy as np
import pandas as pd
import pytest

from analysis.all_monthly_clob_systematic_research_v2 import (
    CandidateHistoryIndex,
    candidate_history_stats,
    candidate_micro_trades,
)


def _rows(count=40):
    rows = []
    for i in range(count):
        rows.append(
            {
                "btc_move_1m": -25.0,
                "btc_move_2m": -20.0,
                "btc_move_4m": 40.0,
                "regime": "stable",
                "up_price_bucket_2m": "0.2~0.4",
                "liq_sign_up_2m": "mid",
                "spread_sign_2m": "tight",
                "down_price_bucket_2m": "0.4~0.6",
                "liq_sign_down_2m": "mid",
                "imb_sign_2m": "neg",
                "realized_pnl_buy_down_1m": 0.03 if i % 4 else -0.01,
                "realized_pnl_buy_up_2m": 0.02 if i % 5 else -0.02,
                "realized_pnl_buy_down_2m": 0.01 if i % 3 else -0.015,
                "realized_pnl_buy_up_4m": 0.025 if i % 6 else -0.02,
            }
        )
    return pd.DataFrame(rows)


def test_incremental_index_matches_prefix_filtering():
    frame = _rows()
    index = CandidateHistoryIndex()

    for i, row in frame.iterrows():
        history = frame.iloc[:i]
        for micro, side, minute in candidate_micro_trades(row):
            expected = candidate_history_stats(history, row, micro, side, minute)
            actual = index.stats(row, micro, side, minute)
            assert actual[2] == expected[2]
            if np.isnan(expected[0]):
                assert np.isnan(actual[0])
                assert np.isnan(actual[1])
            else:
                assert actual[0] == pytest.approx(expected[0])
                assert actual[1] == pytest.approx(expected[1])
        index.update(row)

