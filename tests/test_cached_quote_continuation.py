"""Continuation invariants on tiny invented books, never archived inputs."""
import unittest

import pandas as pd

from analysis import cached_quote_execution as execution
from analysis import timed_quote_execution as reference


START = pd.Timestamp("2024-01-01T00:00:00Z")
PARAMETERS = dict(entry_delay_seconds=1., latency_seconds=2., hold_seconds=5.,
                  max_quote_delay_seconds=1., cash_release_delay_after_close_seconds=20.)


def row(number, *, decision=None, prediction=.8):
    decision = START + pd.Timedelta(seconds=100+500*number) if decision is None else decision
    return {"market_id": f"toy::m-{number}", "run_name": "toy", "slug": f"m-{number}",
            "side": "up", "decision_ts": decision,
            "market_close_ts": decision + pd.Timedelta(seconds=100),
            "decision_ask_price": .5, "decision_sell_size": 1000.,
            "predicted_exit_bid_price": prediction}


def fixture(rows, *, exit_bids=None, thin=None, missing=None):
    books = {}
    for i, candidate in enumerate(rows):
        entry = candidate["decision_ts"]+pd.Timedelta(seconds=3)
        target = entry+pd.Timedelta(seconds=7)
        price = (exit_bids or {}).get(i, 60.)
        size = 0. if i in (thin or set()) else 10000.
        quotes = []
        for stamp, bid, depth in [(entry, 60., 10000.), (target, price, size)]:
            if stamp == target and i in (missing or set()):
                continue
            quotes.append({"ts_utc": stamp, "buy_up_cents": 50., "buy_down_cents": 50.,
                "sell_up_cents": bid, "sell_down_cents": bid,
                "buy_up_size": depth, "buy_down_size": depth,
                "sell_up_size": depth, "sell_down_size": depth, "quote_conflict": False})
        books[candidate["market_id"]] = pd.DataFrame(quotes)
    return books


class Source:
    def __init__(self, books):
        self.books = books
        self.calls = []

    def select(self, market_id, target_ts, close_ts, **kwargs):
        self.calls.append((market_id, kwargs["kind"], target_ts))
        return reference.select_book_quote(self.books.get(market_id), target_ts, close_ts, **kwargs)


def play(rows, books, **kwargs):
    source = Source(books)
    result = execution.replay_cached_quotes(pd.DataFrame(rows), ["toy"], source, **PARAMETERS, **kwargs)
    return result, source


class ContinuationTests(unittest.TestCase):
    def test_default_entire_mapping_and_scope_empty_behavior_are_unchanged(self):
        rows = [row(i) for i in range(12)]
        books = fixture(rows)
        expected = reference.replay_timed_quotes(pd.DataFrame(rows), ["toy"], books, **PARAMETERS)
        actual, source = play(rows, books)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["fills"], 10)
        self.assertFalse(any(mid == rows[10]["market_id"] for mid, _, _ in source.calls))
        outside = pd.DataFrame([{**row(0), "run_name": "outside"}])
        self.assertEqual(execution.replay_cached_quotes(outside, ["toy"], Source({}), **PARAMETERS),
                         reference.replay_timed_quotes(outside, ["toy"], {}, **PARAMETERS))

    def test_twelve_positions_one_loop_prefix_matches_exact_default(self):
        rows = [row(i) for i in range(12)]
        books = fixture(rows)
        expected, _ = play(rows, books)
        snapshots = []
        actual, source = play(rows, books, continue_after_required_fills=True,
                              required_prefix_observer=lambda value: snapshots.append(value))
        self.assertEqual(snapshots, [expected])
        self.assertEqual(actual["required_prefix_result"], expected)
        self.assertEqual(actual["fills"], actual["resolved_fills"])
        self.assertEqual(actual["fills"], 12)
        self.assertEqual(actual["first_ten_fills"], 10)
        self.assertEqual(actual["first_ten_ending_cash"], expected["ending_cash"])
        self.assertEqual(actual["processed_candidate_count"], 12)
        self.assertEqual(actual["required_prefix_processed_candidate_count"], 10)
        self.assertEqual(actual["candidate_stream_count"], 12)
        self.assertTrue(actual["candidate_stream_exhausted"])
        self.assertTrue(actual["full_sequence_net_growth"])
        self.assertIsNone(actual["failure_reason"])
        self.assertEqual(len(source.calls), 24)
        self.assertEqual(actual["chain"][:10], expected["chain"])

    def test_guard_rejects_before_next_candidate_check_or_quote(self):
        rows = [row(i) for i in range(11)]
        rows[-1]["side"] = "invalid-after-prefix"
        source = Source(fixture(rows))
        seen = []
        def reject(snapshot):
            seen.append(snapshot)
            self.assertEqual(len(source.calls), 20)
            raise ValueError("frozen_prefix_guard_rejected")
        with self.assertRaisesRegex(ValueError, "frozen_prefix_guard_rejected"):
            execution.replay_cached_quotes(pd.DataFrame(rows), ["toy"], source, **PARAMETERS,
                continue_after_required_fills=True, required_prefix_observer=reject)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(source.calls), 20)

    def test_virtual_payout_does_not_unlock_pending_or_pay_twice(self):
        rows = [row(i) for i in range(10)]
        release = rows[-1]["market_close_ts"]+pd.Timedelta(seconds=20)
        rows += [row(10, decision=release-pd.Timedelta(seconds=3)),
                 row(11, decision=release-pd.Timedelta(seconds=2))]
        snapshots = []
        result, source = play(rows, fixture(rows), continue_after_required_fills=True,
            required_prefix_observer=lambda x: snapshots.append(x))
        prefix = snapshots[0]
        self.assertEqual(result["fills"], 11)
        self.assertEqual(result["attempts"][-1]["status"], "prior_cash_not_released")
        self.assertEqual(result["attempts"][-1]["cash_unchanged"], prefix["chain"][-1]["cash_after_entry"])
        self.assertEqual(result["chain"][-1]["cash_before_entry"], prefix["ending_cash"])
        self.assertEqual(result["ending_cash"], result["chain"][-1]["cash_after_entry"]+result["chain"][-1]["payout"])
        self.assertFalse(any(mid == rows[10]["market_id"] for mid, _, _ in source.calls))
        self.assertEqual(result["processed_candidate_count"], 12)

    def test_prefix_is_deeply_isolated_from_observer_and_continuing_chain(self):
        rows = [row(i) for i in range(12)]
        expected, _ = play(rows, fixture(rows))
        def mutate(snapshot):
            snapshot["chain"][0]["entry_quote_evidence"]["ask_price"] = -99
            snapshot["chain"].clear()
            snapshot["attempts"].append({"invented": True})
        result, _ = play(rows, fixture(rows), continue_after_required_fills=True,
                          required_prefix_observer=mutate)
        self.assertEqual(result["required_prefix_result"], expected)
        self.assertEqual(result["chain"][0], expected["chain"][0])
        result["chain"][0]["entry_quote_evidence"]["ask_price"] = -7
        self.assertEqual(result["required_prefix_result"], expected)

    def test_later_loss_and_lock_cannot_rewrite_successful_first_ten(self):
        rows = [row(i) for i in range(13)]
        books = fixture(rows, exit_bids={10: 48.}, thin={11})
        expected, _ = play(rows, books)
        result, source = play(rows, books, continue_after_required_fills=True,
                              required_prefix_observer=lambda x: None)
        self.assertLess(result["chain"][10]["realized_pnl"], 0)
        self.assertEqual(result["required_prefix_result"], expected)
        self.assertTrue(result["strict_net_growth_ten"])
        self.assertTrue(result["ten_resolved_sequence_complete"])
        self.assertFalse(result["full_sequence_resolved"])
        self.assertFalse(result["full_sequence_net_growth"])
        self.assertEqual(result["continuation_failure_reason"], "exit_price_or_depth_invalid")
        self.assertEqual(result["failure_reason"], "exit_price_or_depth_invalid")
        self.assertGreater(result["locked_shares"], 0)
        self.assertIsNone(result["chain"][-1]["payout"])
        self.assertEqual(result["processed_candidate_count"], 12)
        self.assertFalse(result["candidate_stream_exhausted"])
        self.assertFalse(any(mid == rows[-1]["market_id"] for mid, _, _ in source.calls))

    def test_prefix_false_stays_false_even_if_later_cash_grows(self):
        rows = [row(i) for i in range(12)]
        books = fixture(rows, exit_bids={i: 54. for i in range(10)})
        expected, _ = play(rows, books)
        self.assertEqual(expected["fills"], 10)
        self.assertFalse(expected["first_ten_passed"])
        result, _ = play(rows, books, continue_after_required_fills=True,
                          required_prefix_observer=lambda x: None)
        self.assertEqual(result["required_prefix_result"], expected)
        self.assertFalse(result["first_ten_passed"])
        self.assertEqual(result["first_ten_ending_cash"], expected["ending_cash"])
        self.assertGreater(result["ending_cash"], result["first_ten_ending_cash"])

    def test_processed_count_includes_rejections_before_prefix_and_after(self):
        rows = [row(0, prediction=.5)]+[row(i) for i in range(1, 12)]
        rows[-1]["predicted_exit_bid_price"] = .5
        snapshots = []
        result, _ = play(rows, fixture(rows), continue_after_required_fills=True,
                          required_prefix_observer=lambda x: snapshots.append(x))
        self.assertEqual(result["processed_candidate_count"], 12)
        self.assertEqual(result["required_prefix_processed_candidate_count"], 11)
        self.assertEqual(result["required_prefix_result"]["fills"], 10)
        self.assertEqual(len(result["required_prefix_result"]["attempts"]), 1)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(len(snapshots), 1)

    def test_short_empty_or_locked_prefix_never_invents_a_snapshot(self):
        for count, thin in [(0, set()), (3, set()), (10, {9})]:
            with self.subTest(count=count, thin=thin):
                rows = [row(i) for i in range(count)]
                snapshots = []
                result, _ = play(rows, fixture(rows, thin=thin), continue_after_required_fills=True,
                                  required_prefix_observer=lambda x: snapshots.append(x))
                self.assertEqual(snapshots, [])
                self.assertIsNone(result["required_prefix_result"])
                self.assertIsNone(result["required_prefix_processed_candidate_count"])
                self.assertFalse(result["ten_resolved_sequence_complete"])
                self.assertEqual(result["first_ten_fills"], count)
                self.assertTrue(result["candidate_stream_exhausted"])

    def test_opt_in_validation_keeps_original_required_ten_contract(self):
        for kw in [{"continue_after_required_fills": True},
                   {"continue_after_required_fills": 1, "required_prefix_observer": lambda x: None},
                   {"required_prefix_observer": lambda x: None},
                   {"continue_after_required_fills": True, "required_prefix_observer": lambda x: None, "required_fills": 11}]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                play([], {}, **kw)


if __name__ == "__main__":
    unittest.main()
