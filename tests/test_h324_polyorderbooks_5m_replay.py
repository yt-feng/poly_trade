import tempfile
import unittest
from pathlib import Path

import pandas as pd

from analysis.h324_polyorderbooks_5m_replay import _shares, _taker_fee, load_entries, replay


def _row(market, token, outcome, winner, tte=120.0, ask=0.40, bid=0.39, pressure=0.6):
    if pressure > 0:
        bid_size, ask_size = 200.0, 50.0
    else:
        bid_size, ask_size = 50.0, 200.0
    captured = pd.Timestamp("2026-10-01T00:03:00Z") + pd.Timedelta(minutes=int(market))
    end = captured + pd.Timedelta(seconds=120)
    return {
        "market_slug": f"btc-updown-5m-{market}", "coin": "btc", "contract_length": "5m",
        "token_id": f"token-{market}-{token}", "outcome": outcome,
        "is_winning_outcome": winner, "market_end_at": end, "captured_at": captured,
        "seconds_to_close": tte, "bid_sizes": [bid_size], "ask_sizes": [ask_size],
        "bid_prices": [bid], "ask_prices": [ask], "best_bid": bid, "best_ask": ask,
        "crossed": False,
    }


class H324Tests(unittest.TestCase):
    def test_uses_price_dependent_polymarket_taker_fee(self):
        self.assertEqual(_taker_fee(100.0, 0.50), 1.75)
        self.assertEqual(_taker_fee(5.0, 0.99), 0.00347)
        self.assertEqual(_taker_fee(5.0, 0.01), 0.00347)

    def test_share_budget_accounts_for_each_complement_leg_fee(self):
        shares = _shares(10.0, [0.40, 0.40], 1.0)
        self.assertEqual(shares, 11.996)

    def test_load_entries_enforces_two_tokens_and_horizon(self):
        rows = [_row(1, "u", "Up", True), _row(1, "d", "Down", False)]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "btc.parquet"
            pd.DataFrame(rows).to_parquet(path)
            entries, summary = load_entries(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(summary["decision_markets"], 1)

    def test_book_pressure_replay_is_paper_only_diagnostic(self):
        rows = [_row(i, "u", "Up", i % 2 == 0) for i in range(1, 13)]
        rows += [_row(i, "d", "Down", i % 2 == 1, pressure=-0.6) for i in range(1, 13)]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "btc.parquet"
            pd.DataFrame(rows).to_parquet(path)
            entries, _ = load_entries(path)
        result = replay(entries, "book_pressure", 0.20, 100.0)
        self.assertEqual(result["candidate"], "book_pressure")
        self.assertGreaterEqual(result["fills"], 0)
        self.assertNotIn("verified_canary_fills", result)

    def test_rejects_non_five_minute_rows(self):
        rows = [_row(1, "u", "Up", True), _row(1, "d", "Down", False)]
        rows[0]["contract_length"] = "15m"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "btc.parquet"
            pd.DataFrame(rows).to_parquet(path)
            with self.assertRaisesRegex(ValueError, "non_5m_rows"):
                load_entries(path)


if __name__ == "__main__":
    unittest.main()
