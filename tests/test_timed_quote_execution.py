from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

from analysis import timed_quote_execution as execution


START = pd.Timestamp("2026-01-01T00:00:00Z")


def candidate(number=0, *, shift=0, prediction=.8):
    begin = START + pd.Timedelta(seconds=shift)
    return {"market_id": f"run::market-{number}", "run_name": "run", "slug": f"market-{number}",
        "side": "up", "decision_ts": begin + pd.Timedelta(seconds=120),
        "market_close_ts": begin + pd.Timedelta(seconds=300),
        "decision_ask_price": .5, "decision_sell_size": 1000.,
        "predicted_exit_bid_price": prediction}


def book(seconds=(132, 170, 171), *, shift=0, bids=None, asks=None, sizes=None, conflicts=None):
    count = len(seconds)
    return pd.DataFrame({"ts_utc": [START + pd.Timedelta(seconds=shift+s) for s in seconds],
        "buy_up_cents": asks if asks is not None else [50.] * count,
        "buy_down_cents": [50.] * count,
        "sell_up_cents": bids if bids is not None else [70.] * count,
        "sell_down_cents": [40.] * count,
        "buy_up_size": sizes if sizes is not None else [10000.] * count,
        "sell_up_size": sizes if sizes is not None else [10000.] * count,
        "buy_down_size": [10000.] * count, "sell_down_size": [10000.] * count,
        "quote_conflict": conflicts if conflicts is not None else [False] * count})


def replay(rows, books, **kwargs):
    return execution.replay_timed_quotes(pd.DataFrame(rows), ["run"], books, **kwargs)


class TimedQuoteExecutionTests(unittest.TestCase):
    def test_first_valid_price_has_fixed_window_and_does_not_select_depth(self):
        quotes = book((170, 172, 173), bids=[np.nan, 45., 90.], sizes=[10000., 1., 10000.])
        picked, reason = execution.select_book_quote(quotes, START + pd.Timedelta(seconds=170),
            START + pd.Timedelta(seconds=300), side="up", kind="exit")
        self.assertEqual(reason, "selected")
        self.assertEqual(picked.ts_utc, START + pd.Timedelta(seconds=172))
        self.assertEqual(picked.sell_up_size, 1)
        late = book((170, 174), bids=[np.nan, 90.])
        picked, reason = execution.select_book_quote(late, START + pd.Timedelta(seconds=170),
            START + pd.Timedelta(seconds=300), side="up", kind="exit")
        self.assertIsNone(picked)
        self.assertEqual(reason, "invalid_quote_price")

    def test_conflict_is_unknown_without_skipping_and_future_conflict_does_not_poison_prefix(self):
        for bids in ([70., 80.], [np.nan, 80.]):
            with self.subTest(bids=bids):
                quotes = book((170, 171), bids=bids, conflicts=[True, False])
                picked, reason = execution.select_book_quote(quotes, START + pd.Timedelta(seconds=170),
                    START + pd.Timedelta(seconds=300), side="up", kind="exit")
                self.assertIsNone(picked)
                self.assertEqual(reason, "unknown_quote_conflict")
        quotes = book((132, 170, 250), conflicts=[False, False, True])
        result = replay([candidate()], {"run::market-0": quotes})
        self.assertEqual(result["resolved_fills"], 1)
        quotes = book((170, 171, 172), bids=[np.nan, np.nan, 80.], conflicts=[False, True, False])
        picked, reason = execution.select_book_quote(quotes, START + pd.Timedelta(seconds=170),
            START + pd.Timedelta(seconds=300), side="up", kind="exit")
        self.assertIsNone(picked)
        self.assertEqual(reason, "unknown_quote_conflict")

    def test_exact_tolerance_and_market_close_boundaries(self):
        quotes = book((173,))
        picked, _ = execution.select_book_quote(quotes, START + pd.Timedelta(seconds=170),
            START + pd.Timedelta(seconds=300), side="up", kind="exit")
        self.assertIsNotNone(picked)
        picked, _ = execution.select_book_quote(quotes, START + pd.Timedelta(seconds=170),
            START + pd.Timedelta(seconds=173), side="up", kind="exit")
        self.assertIsNone(picked)

    def test_cost_cap_covers_both_sides_and_zero_fee(self):
        for prediction, fee in ((.8, .07), (.8, 0.), (.01, .07), (1., 1.)):
            with self.subTest(prediction=prediction, fee=fee):
                limit = execution.entry_price_cap(prediction, fee_rate=fee)
                sell_price = max(0., prediction-.01)
                budget = max(0., sell_price-fee*sell_price*(1-sell_price)-.02)
                self.assertAlmostEqual(limit+fee*limit*(1-limit), budget)
        for kwargs in ({"predicted_exit_bid_price": np.nan}, {"predicted_exit_bid_price": 1.1},
                       {"predicted_exit_bid_price": .8, "fee_rate": -1},
                       {"predicted_exit_bid_price": .8, "edge_floor": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                execution.entry_price_cap(**kwargs)

    def test_arrival_cap_cancels_before_debit_independently_of_future_bid(self):
        results = []
        for future_bid in (5., 99.):
            quotes = book(bids=[50., future_bid, future_bid], asks=[90., 50., 50.])
            result = replay([candidate()], {"run::market-0": quotes})
            self.assertEqual(result["fills"], 0)
            self.assertEqual(result["ending_cash"], 10.)
            self.assertEqual(result["attempts"][0]["status"], "entry_cost_limit_exceeded")
            results.append(result)
        self.assertEqual(results[0], results[1])

    def test_absent_decision_edge_cannot_be_created_by_a_later_cheap_ask(self):
        row = candidate(prediction=.55)
        quotes = book((120, 132, 170, 171), asks=[50., 20., 40., 40.], bids=[49., 18., 38., 38.])
        self.assertTrue((quotes.sell_up_cents <= quotes.buy_up_cents).all())
        self.assertLess(.21, execution.entry_price_cap(.55))
        result = replay([row], {row["market_id"]: quotes})
        self.assertEqual(result["fills"], 0)
        self.assertEqual(result["ending_cash"], 10.)
        self.assertEqual(result["attempts"][0]["status"], "decision_cost_floor_not_met")
        class UnreadableFuture(dict):
            def get(self, key, default=None):
                raise AssertionError("decision rejection must precede future book access")
        self.assertEqual(replay([row], UnreadableFuture()), result)

    def test_exit_is_anchored_to_actual_entry_and_keeps_a_loss(self):
        quotes = book((132, 133, 170, 171, 172), asks=[np.nan, 50., 50., 50., 50.],
                      bids=[50., 50., 99., 45., 99.])
        result = replay([candidate()], {"run::market-0": quotes})
        event = result["chain"][0]
        self.assertEqual(event["entry_ts"], "2026-01-01T00:02:13+00:00")
        self.assertEqual(event["exit_target_ts"], "2026-01-01T00:02:51+00:00")
        self.assertEqual(event["exit_ts"], event["exit_target_ts"])
        self.assertAlmostEqual(event["exit_fill_price"], .44)
        self.assertLess(event["realized_pnl"], 0)
        self.assertLess(result["ending_cash"], 10)
        self.assertEqual(event["status"], "timed_quote_exit")
        self.assertAlmostEqual(event["entry_fee"], event["shares"]*.07*.51*.49)
        self.assertAlmostEqual(event["exit_fee"], event["shares"]*.07*.44*.56)
        self.assertAlmostEqual(result["ending_cash"], event["cash_after_entry"]+event["payout"])
        self.assertEqual(result["ending_cash_available_ts"], "2026-01-01T00:20:00+00:00")

    def test_entry_missing_conflict_or_insufficient_depth_does_not_debit(self):
        cases = [book((140,)), book(conflicts=[True, False, False]),
                 book((132, 133, 170), sizes=[1., 10000., 10000.])]
        for quotes in cases:
            with self.subTest(quotes=quotes.to_dict()):
                result = replay([candidate()], {"run::market-0": quotes})
                self.assertEqual(result["fills"], 0)
                self.assertEqual(result["ending_cash"], 10.)
                self.assertEqual(result["locked_shares"], 0.)

    def test_unknown_or_insufficient_exit_locks_and_never_uses_terminal_label(self):
        cases = [book((132, 174)), book(conflicts=[False, True, False]),
                 book(sizes=[10000., 1., 10000.]), book(bids=[70., np.nan, np.nan]),
                 book(bids=[70., .5, 99.])]
        row = candidate(); row.update(outcome_up=1., label_observed_ts=START + pd.Timedelta(seconds=300))
        following = candidate(1, shift=1300)
        for quotes in cases:
            with self.subTest(quotes=quotes.to_dict()):
                result = replay([row, following], {row["market_id"]: quotes,
                    following["market_id"]: book(shift=1300)})
                self.assertEqual(result["fills"], 1)
                self.assertEqual(result["resolved_fills"], 0)
                self.assertGreater(result["locked_shares"], 0)
                self.assertLess(result["ending_cash"], 1e-9)
                self.assertIsNone(result["chain"][0]["payout"])
                self.assertEqual(result["chain"][0]["status"], "unresolved_position")

    def test_loss_can_continue_and_pending_cash_blocks_early_candidate(self):
        rows = [candidate(0), candidate(1, shift=500), candidate(2, shift=1300)]
        books = {row["market_id"]: book(shift=shift, bids=[45., 45., 45.])
                 for row, shift in zip(rows, (0, 500, 1300))}
        result = replay(rows, books)
        self.assertEqual(result["fills"], 2)
        first, second = result["chain"]
        self.assertLess(first["realized_pnl"], 0)
        self.assertLess(second["realized_pnl"], 0)
        self.assertAlmostEqual(second["cash_before_entry"], first["cash_after_entry"]+first["payout"])
        self.assertEqual(result["attempts"][0]["status"], "prior_cash_not_released")

    def test_depth_fraction_matches_frozen_legacy_math(self):
        from analysis import h561_online_depth_proxy as legacy
        for price in (.05, .51, .99):
            for cash in (4.99, 5., 5.2, 10., 100.):
                for depth in (0., 10., 1000.):
                    with self.subTest(price=price, cash=cash, depth=depth):
                        row = SimpleNamespace(fill_price=price, side="up", sell_up_size_2m=depth)
                        expected = legacy.depth_proxy_fraction(row, cash, (3., 5., 7.))
                        actual = execution.depth_proxy_fraction(price, depth, cash)
                        self.assertEqual(actual, expected)

    def test_ten_entered_with_unknown_last_is_not_ten_resolved(self):
        rows = [candidate(i, shift=1300*i) for i in range(11)]
        books = {row["market_id"]: book(shift=1300*i) for i, row in enumerate(rows)}
        complete = replay(rows, books)
        self.assertEqual(complete["fills"], 10)
        self.assertEqual(complete["resolved_fills"], 10)
        self.assertTrue(complete["ten_resolved_sequence_complete"])
        self.assertTrue(complete["first_ten_passed"])
        self.assertTrue(complete["strict_net_growth_ten"])
        books[rows[9]["market_id"]] = book((132,), shift=1300*9)
        incomplete = replay(rows, books)
        self.assertEqual(incomplete["fills"], 10)
        self.assertEqual(incomplete["resolved_fills"], 9)
        self.assertFalse(incomplete["ten_resolved_sequence_complete"])
        self.assertFalse(incomplete["first_ten_passed"])
        self.assertFalse(incomplete["strict_net_growth_ten"])
        self.assertGreater(incomplete["locked_shares"], 0)

    def test_ten_resolved_at_break_even_does_not_pass_net_growth(self):
        rows = [candidate(i, shift=1300*i) for i in range(11)]
        books = {row["market_id"]: book(shift=1300*i, bids=[50., 50., 50.])
                 for i, row in enumerate(rows)}
        result = replay(rows, books, fee_rate=0., slippage=0.)
        self.assertEqual(result["fills"], 10)
        self.assertEqual(result["resolved_fills"], 10)
        self.assertEqual(result["ending_cash"], 10.)
        self.assertTrue(result["ten_resolved_sequence_complete"])
        self.assertFalse(result["first_ten_passed"])
        self.assertFalse(result["strict_net_growth_ten"])
        self.assertEqual(result["failure_reason"], "sequence_or_net_growth_gate_not_met")

    def test_frozen_ledger_and_execution_contract_validation(self):
        for kwargs in ({"initial_cash": 100}, {"required_fills": 9}, {"minimum_order_usd": 1},
                       {"minimum_order_shares": 1}, {"latency_seconds": float("inf")},
                       {"hold_seconds": 0}, {"depth_haircut": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replay([], {}, **kwargs)
        result = replay([], {})
        self.assertEqual(result["ending_cash"], 10)
        self.assertEqual(result["fills"], 0)


if __name__ == "__main__":
    unittest.main()
