import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis'))
from pair_arb_old_data import first_pair_for_market, replay_same_ledger


class PairArbFieldContract(unittest.TestCase):
    def rows(self, up, down, bid_up=0.47, bid_down=0.48):
        return pd.DataFrame([
            {'slug': 'btc-updown-5m-1792195200', 'run_name': 'r', 'ts': pd.Timestamp('2026-10-17T00:00:00Z'),
             'buy_up_cents': up * 100, 'buy_down_cents': down * 100,
             'buy_up_size': 1000, 'buy_down_size': 1000,
             'sell_up_cents': bid_up * 100, 'sell_down_cents': bid_down * 100,
             'sell_up_size': 1000, 'sell_down_size': 1000},
            {'slug': 'btc-updown-5m-1792195200', 'run_name': 'r', 'ts': pd.Timestamp('2026-10-17T00:00:04Z'),
             'buy_up_cents': up * 100, 'buy_down_cents': down * 100,
             'buy_up_size': 1000, 'buy_down_size': 1000,
             'sell_up_cents': bid_up * 100, 'sell_down_cents': bid_down * 100,
             'sell_up_size': 1000, 'sell_down_size': 1000},
        ])

    def test_uses_buy_fields_as_taker_asks(self):
        result = first_pair_for_market(self.rows(0.49, 0.50), 4, 5, 0, 0)
        self.assertIsNotNone(result)

    def test_sell_bid_sum_cannot_create_false_arbitrage(self):
        result = first_pair_for_market(self.rows(0.52, 0.52), 4, 5, 0, 0)
        self.assertIsNone(result)

    def test_same_ledger_releases_pair_cash_before_next_entry(self):
        candidates = pd.DataFrame([
            {
                'slug': 'btc-updown-5m-1792195200',
                'fill_ts': '2026-10-17T00:00:00Z',
                'cost': 10.0, 'fees': 0.0, 'shares': 10.0,
            },
            {
                'slug': 'btc-updown-5m-1792195200',
                'fill_ts': '2026-10-17T00:05:00Z',
                'cost': 1.0, 'fees': 0.0, 'shares': 1.0,
            },
            {
                'slug': 'btc-updown-5m-1792195500',
                'fill_ts': '2026-10-17T00:20:00Z',
                'cost': 10.0, 'fees': 0.0, 'shares': 11.0,
            },
        ])
        result = replay_same_ledger(candidates, initial_cash=10.0, cash_release_delay_seconds=600)
        self.assertEqual(result['same_ledger_trades'], 2)
        self.assertEqual(result['blocked'], 1)
        self.assertAlmostEqual(result['ending_cash'], 11.0)
        self.assertTrue(result['passed_ten'] is False)


if __name__ == '__main__':
    unittest.main()
