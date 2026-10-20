import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from maker_path_efficiency_shadow import inject_maker_execution, maker_touch_mask
from maker_shadow_chain import attach_maker_snapshots


class MakerPathEfficiencyShadowTests(unittest.TestCase):
    def test_touch_mask_follows_selected_side_and_minute(self):
        frame = pd.DataFrame(
            {
                "v1_adaptive_mix_side": ["buy_up", "buy_down", "buy_up"],
                "v1_adaptive_mix_minute": [1, 2, 4],
                "maker_buy_up_1m_d4_touch": [1, 0, 0],
                "maker_buy_down_2m_d4_touch": [0, 1, 0],
                "maker_buy_up_4m_d4_touch": [0, 0, 1],
            }
        )
        self.assertEqual(maker_touch_mask(frame).tolist(), [True, True, True])

    def test_execution_mapping_uses_maker_bid_and_zero_adverse_slippage(self):
        frame = pd.DataFrame(
            {
                "v1_adaptive_mix_side": ["buy_up"],
                "v1_adaptive_mix_minute": [1],
                "maker_buy_up_1m_d4_price": [0.41],
                "maker_buy_up_1m_d4_size": [250.0],
                "maker_buy_up_1m_d4_spread": [0.02],
                "maker_buy_up_1m_d4_overround": [0.01],
            }
        )
        mapped = inject_maker_execution(frame)
        self.assertEqual(mapped.loc[0, "exec_buy_up_1m_d4_quote_price"], 0.41)
        self.assertEqual(mapped.loc[0, "exec_buy_up_1m_d4_signal_price"], 0.41)
        self.assertEqual(mapped.loc[0, "exec_buy_up_1m_d4_quote_size"], 250.0)
        self.assertEqual(mapped.loc[0, "exec_buy_up_1m_d4_adverse_slippage"], 0.0)

    def test_trade_through_requires_later_ask_one_tick_below_resting_bid(self):
        first = pd.Timestamp("2026-01-01T00:00:00Z")
        features = pd.DataFrame({"market_id": ["m"], "first_quote_ts": [first]})
        quotes = pd.DataFrame(
            {
                "market_id": ["m", "m", "m"],
                "ts_utc": [first, first + pd.Timedelta(minutes=1), first + pd.Timedelta(minutes=1, seconds=4)],
                "close_ts_utc": [first + pd.Timedelta(minutes=5)] * 3,
                "sell_up_cents": [41, 41, 40],
                "buy_up_cents": [43, 43, 40],
                "sell_up_size": [200, 200, 200],
                "spread_up_cents": [2, 2, 1],
                "mid_overround_cents": [1, 1, 1],
                "sell_down_cents": [50, 50, 50],
                "buy_down_cents": [52, 52, 52],
                "sell_down_size": [200, 200, 200],
                "spread_down_cents": [2, 2, 2],
            }
        )
        result = attach_maker_snapshots(features, quotes, delays=(4,), minutes=(1,))
        self.assertEqual(int(result.loc[0, "maker_buy_up_1m_d4_touch"]), 1)

        quotes.loc[2, "buy_up_cents"] = 41
        result = attach_maker_snapshots(features, quotes, delays=(4,), minutes=(1,))
        self.assertEqual(int(result.loc[0, "maker_buy_up_1m_d4_touch"]), 0)

    def test_touch_sensitivity_can_use_zero_cent_crossing_and_distinct_delay(self):
        first = pd.Timestamp("2026-01-01T00:00:00Z")
        features = pd.DataFrame({"market_id": ["m"], "first_quote_ts": [first]})
        quotes = pd.DataFrame(
            {
                "market_id": ["m", "m", "m"],
                "ts_utc": [first, first + pd.Timedelta(minutes=1), first + pd.Timedelta(minutes=1, seconds=30)],
                "close_ts_utc": [first + pd.Timedelta(minutes=5)] * 3,
                "sell_up_cents": [41, 41, 41],
                "buy_up_cents": [43, 43, 41],
                "sell_up_size": [200, 200, 200],
                "spread_up_cents": [2, 2, 0],
                "mid_overround_cents": [1, 1, 1],
                "sell_down_cents": [50, 50, 50],
                "buy_down_cents": [52, 52, 52],
                "sell_down_size": [200, 200, 200],
                "spread_down_cents": [2, 2, 2],
            }
        )
        result = attach_maker_snapshots(
            features,
            quotes,
            delays=(30,),
            minutes=(1,),
            crossing_cents=0.0,
        )
        self.assertEqual(int(result.loc[0, "maker_buy_up_1m_d30_touch"]), 1)


if __name__ == "__main__":
    unittest.main()
