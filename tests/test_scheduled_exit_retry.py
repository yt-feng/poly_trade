"""Tiny synthetic books only; no empirical tables, estimators or network."""
from copy import deepcopy
import json
import unittest

import numpy as np
import pandas as pd

from analysis import cached_quote_execution as ledger
from analysis import scheduled_exit_retry as retry
from analysis import timed_quote_execution as reference


START = pd.Timestamp("2025-01-01T00:00:00Z")


def at(seconds):
    return START + pd.Timedelta(seconds=seconds)


def policy(**changes):
    return retry.RetryPolicy(**{**dict(reaction_delay_seconds=2., order_latency_seconds=3.,
        max_quote_delay_seconds=2., deadline_after_original_target_seconds=13.,
        max_extra_attempts=2), **changes})


def request(**changes):
    return retry.ExitRequest(**{**dict(market_id="sample::item", side="up",
        original_exit_target_ts=at(20), market_close_ts=at(100), held_shares=6.,
        slippage=.02, depth_haircut=.5, max_quote_delay_seconds=2., order_latency_seconds=3.),
        **changes})


def book(rows):
    """Rows are (seconds, bid-cents, side-size[, conflict])."""
    return pd.DataFrame([{"ts_utc": at(row[0]), "sell_up_cents": row[1], "sell_up_size": row[2],
        "sell_down_cents": row[1], "sell_down_size": row[2],
        "buy_up_cents": 40., "buy_down_cents": 40.,
        "buy_up_size": 10000., "buy_down_size": 10000.,
        "quote_conflict": row[3] if len(row) > 3 else False} for row in rows])


class Source:
    def __init__(self, rows):
        self.book = book(rows)
        self.calls = []

    def select(self, market_id, target_ts, close_ts, **kwargs):
        self.calls.append((market_id, target_ts, close_ts, dict(kwargs)))
        return reference.select_book_quote(self.book, target_ts, close_ts, **kwargs)


def resolve(rows, *, req=None, pol=None):
    req, pol = req or request(), pol or policy()
    source = Source(rows)
    quote, reason = source.select(req.market_id, req.original_exit_target_ts,
        req.market_close_ts, side=req.side, kind="exit", max_quote_delay_seconds=req.max_quote_delay_seconds)
    result = retry.ScheduledExitResolver(pol)(req, quote, reason, source)
    return result, source, quote


def candidate(number=0, shift=0, prediction=.85):
    return {"market_id": f"sample::item-{number}", "run_name": "sample", "slug": f"item-{number}",
        "side": "up", "decision_ts": at(100+shift), "market_close_ts": at(300+shift),
        "decision_ask_price": .4, "decision_sell_size": 1000., "predicted_exit_bid_price": prediction}


def replay(rows, quotes, *, resolver=None):
    return ledger.replay_cached_quotes(pd.DataFrame(rows), ["sample"], quotes,
        entry_delay_seconds=1., latency_seconds=3., hold_seconds=10., max_quote_delay_seconds=2.,
        slippage=.02, depth_haircut=.5, cash_release_delay_after_close_seconds=50.,
        exit_resolver=resolver)


class ScheduledRetryTests(unittest.TestCase):
    def test_original_full_size_has_no_retry_or_future_quality_read(self):
        result, source, original = resolve([(20, 40, 12), (25, 99, 10000)])
        self.assertIs(result.quote, original)
        self.assertFalse(result.resolved_by_retry)
        self.assertEqual(result.trace, ())
        self.assertEqual(len(source.calls), 1)

    def test_losing_first_full_size_beats_later_profitable_quote(self):
        result, source, _ = resolve([(20, 60, 1), (24.9, 99, 10000),
                                    (25, 30, 12), (25.1, 90, 10000)])
        self.assertTrue(result.resolved_by_retry)
        self.assertEqual(result.quote["ts_utc"], at(25))
        self.assertEqual(result.quote["sell_up_cents"], 30)
        self.assertEqual(result.terminal_reason, "retry_full_size")
        self.assertEqual(source.calls[1][1:3], (at(25), at(33)))
        evidence = result.to_evidence()
        self.assertEqual(evidence["selected_attempt_quote_delay_seconds"], 0.)
        self.assertEqual(evidence["trace"][0]["decision_ts"], at(17).isoformat())
        self.assertIsNone(evidence["trace"][0]["failure_observed_ts"])
        self.assertEqual(evidence["trace"][1]["failure_observed_ts"], at(20).isoformat())
        json.dumps(evidence, allow_nan=False)

    def test_selected_thin_is_not_skipped_inside_window(self):
        result, source, _ = resolve([(20, 50, 1), (25, 60, 1),
                                    (25.1, 99, 10000), (30, 35, 12)])
        self.assertTrue(result.resolved_by_retry)
        self.assertEqual(result.quote["ts_utc"], at(30))
        self.assertEqual([row["status"] for row in result.trace], ["thin", "thin", "full_size"])
        self.assertEqual([call[1] for call in source.calls], [at(20), at(25), at(30)])

    def test_no_original_quote_is_not_rescued(self):
        for rows in ([(25, 90, 10000)], [(20, np.nan, 10000), (25, 90, 10000)]):
            with self.subTest(rows=rows):
                result, source, original = resolve(rows)
                self.assertIsNone(original)
                self.assertIsNone(result.quote)
                self.assertEqual(result.trace, ())
                self.assertEqual(len(source.calls), 1)

    def test_noquote_retry_stops_without_another_attempt(self):
        result, source, original = resolve([(20, 60, 1), (30, 90, 10000)])
        self.assertIs(result.quote, original)
        self.assertFalse(result.resolved_by_retry)
        self.assertEqual(result.terminal_reason, "retry_no_quote_in_window")
        self.assertEqual(len(source.calls), 2)
        self.assertIsNone(result.trace[-1]["selected_quote_ts"])

    def test_conflict_never_advances_to_later_clean_quote(self):
        for rows, expected_calls in [([(20, 60, 1, True), (25, 90, 10000)], 1),
                ([(20, 60, 1), (25, 60, 1, True), (25.1, 90, 10000)], 2)]:
            with self.subTest(expected_calls=expected_calls):
                result, source, original = resolve(rows)
                self.assertFalse(result.resolved_by_retry)
                self.assertIs(result.quote, original)
                self.assertEqual(len(source.calls), expected_calls)
                self.assertIn("unknown_quote_conflict", result.terminal_reason)

    def test_invalid_selected_size_or_adjusted_price_is_not_retry_eligible(self):
        for size in (np.nan, float("inf"), -1., True):
            with self.subTest(size=size):
                result, source, _ = resolve([(20, 60, size), (25, 90, 10000)])
                self.assertEqual(result.trace, ())
                self.assertEqual(len(source.calls), 1)
                self.assertEqual(result.terminal_reason, "original_invalid_quote_size")
        result, source, _ = resolve([(20, 1, 1), (25, 90, 10000)])
        self.assertEqual(result.trace, ())
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result.terminal_reason, "original_invalid_quote_price")
        for bad in ((25, 60, np.nan), (25, 1, 1)):
            result, source, _ = resolve([(20, 60, 1), bad, (30, 90, 10000)])
            self.assertEqual(len(source.calls), 2)
            self.assertFalse(result.resolved_by_retry)

    def test_zero_is_known_thin_and_strict_capacity_equality_has_no_epsilon(self):
        threshold = 12.
        for size, resolved in [(0., False), (np.nextafter(threshold, -np.inf), False),
                               (threshold, True), (np.nextafter(threshold, np.inf), True)]:
            with self.subTest(size=size):
                result, source, _ = resolve([(20, 60, 0), (25, 60, size)],
                                            pol=policy(max_extra_attempts=1))
                self.assertEqual(result.resolved_by_retry, resolved)
                self.assertEqual(len(source.calls), 2)
        result, _, _ = resolve([(20, 60, 1), (25, 60, 1), (30, 60, 1)])
        self.assertEqual(result.terminal_reason, "retry_attempt_budget_exhausted")

    def test_request_endpoints_and_exclusive_deadline_or_close(self):
        for stamp, succeeds in [(25, True), (27, True), (27.000000001, False)]:
            with self.subTest(stamp=stamp):
                result, _, _ = resolve([(20, 60, 1), (stamp, 60, 10000)],
                                       pol=policy(max_extra_attempts=1))
                self.assertEqual(result.resolved_by_retry, succeeds)
        for limit in (25, 26, 27):
            with self.subTest(limit=limit):
                req = request(market_close_ts=at(limit))
                result, source, _ = resolve([(20, 60, 1), (limit, 60, 10000)], req=req)
                self.assertFalse(result.resolved_by_retry)
                self.assertEqual(len(source.calls), 1 if limit == 25 else 2)
                if len(source.calls) == 2:
                    self.assertFalse(result.trace[-1]["window_end_inclusive"])
        result, _, _ = resolve([(20, 60, 1), (25, 60, 10000)],
                               pol=policy(deadline_after_original_target_seconds=5))
        self.assertEqual(result.terminal_reason, "retry_deadline_reached")

    def test_failure_clock_is_selected_quote_not_original_arrival_or_window_end(self):
        result, source, _ = resolve([(21.25, 60, 1), (26.25, 60, 10000)])
        self.assertTrue(result.resolved_by_retry)
        self.assertEqual(source.calls[1][1], at(26.25))
        self.assertEqual(result.trace[-1]["decision_ts"], at(23.25).isoformat())
        result, source, _ = resolve([(20, np.nan, 1), (21, 60, 1),
                                    (26, np.nan, 10000), (27, 60, 1), (27.1, 60, 10000)],
                                    pol=policy(max_extra_attempts=1))
        self.assertFalse(result.resolved_by_retry)
        self.assertEqual(result.trace[-1]["selected_quote_ts"], at(27).isoformat())

    def test_new_retry_delay_preserves_nanoseconds_without_changing_legacy_fields(self):
        result, source, _ = resolve([(20, 60, 1), (25.000000001, 60, 10000)])
        self.assertTrue(result.resolved_by_retry)
        self.assertEqual(result.to_evidence()["selected_attempt_quote_delay_seconds"], 1e-9)
        self.assertEqual(result.trace[-1]["selected_quote_ts"], at(25.000000001).isoformat())

    def test_policy_and_clock_validation_and_selected_source_integrity(self):
        for changes in ({"reaction_delay_seconds": True}, {"order_latency_seconds": np.nan},
                {"max_extra_attempts": False}, {"max_extra_attempts": 0},
                {"reaction_delay_seconds": 0, "order_latency_seconds": 0},
                {"deadline_after_original_target_seconds": -1}, {"max_quote_delay_seconds": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                policy(**changes)
        for changes in ({"held_shares": 0}, {"depth_haircut": 2}, {"slippage": True},
                        {"original_exit_target_ts": "2025-01-01"}, {"side": "other"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                request(**changes)
        for changes in ({"order_latency_seconds": 4}, {"max_quote_delay_seconds": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resolve([(20, 60, 1)], pol=policy(**changes))
        source = Source([(20, 60, 1)])
        bad = source.book.iloc[0].copy()
        bad["ts_utc"] = at(19)
        with self.assertRaises(ValueError):
            retry.ScheduledExitResolver(policy())(request(), bad, "selected", source)
        bad["ts_utc"], bad["quote_conflict"] = at(20), True
        with self.assertRaises(ValueError):
            retry.ScheduledExitResolver(policy())(request(), bad, "selected", source)


class LedgerHookTests(unittest.TestCase):
    def test_disabled_default_and_successful_callback_are_exact_reference(self):
        rows = [candidate()]
        source = Source([(104, 60, 10000), (117, 60, 10000)])
        expected = reference.replay_timed_quotes(pd.DataFrame(rows), ["sample"],
            {rows[0]["market_id"]: source.book}, entry_delay_seconds=1., latency_seconds=3.,
            hold_seconds=10., max_quote_delay_seconds=2., slippage=.02, depth_haircut=.5,
            cash_release_delay_after_close_seconds=50.)
        original = replay(rows, source)
        treatment = replay(rows, source, resolver=retry.ScheduledExitResolver(policy()))
        self.assertEqual(original, expected)
        self.assertEqual(treatment, expected)
        self.assertNotIn("exit_retry", treatment["chain"][0])

    def test_recovery_loss_uses_original_accounting_target_and_new_arrival_evidence(self):
        source = Source([(104, 60, 10000), (117, 60, 1), (122, 30, 10000), (123, 90, 10000)])
        baseline = replay([candidate()], source)
        treatment = replay([candidate()], source, resolver=retry.ScheduledExitResolver(policy()))
        self.assertEqual(baseline["resolved_fills"], 0)
        self.assertEqual(treatment["resolved_fills"], 1)
        event = treatment["chain"][0]
        self.assertLess(event["realized_pnl"], 0)
        self.assertEqual(event["exit_target_ts"], at(117).isoformat())
        self.assertEqual(event["exit_quote_evidence"]["target_ts"], at(117).isoformat())
        self.assertEqual(event["exit_quote_evidence"]["quote_delay_seconds"], 5.)
        self.assertEqual(event["exit_retry"]["selected_attempt_arrival_ts"], at(122).isoformat())
        self.assertEqual(event["exit_retry"]["selected_attempt_quote_delay_seconds"], 0.)
        self.assertEqual(event["cash_release_ts"], at(350).isoformat())
        json.dumps(treatment, allow_nan=False)

    def test_unresolved_keeps_original_thin_quote_and_legacy_lock(self):
        source = Source([(104, 60, 10000), (117, 60, 1), (122, 60, 1), (127, 60, 1)])
        baseline = replay([candidate()], source)
        treatment = replay([candidate()], source, resolver=retry.ScheduledExitResolver(policy()))
        extra = treatment["chain"][0].pop("exit_retry")
        self.assertEqual(treatment, baseline)
        self.assertEqual(len(extra["trace"]), 3)
        self.assertEqual(extra["terminal_reason"], "retry_attempt_budget_exhausted")

    def test_compounded_cash_and_pending_release_keep_one_chain(self):
        rows = [candidate(0), candidate(1, shift=246), candidate(2, shift=247)]
        source = Source([(104, 60, 10000), (117, 60, 1), (122, 70, 10000),
                         (350, 60, 10000), (351, 60, 10000), (364, 50, 10000)])
        result = replay(rows, source, resolver=retry.ScheduledExitResolver(policy()))
        self.assertEqual(len(result["chain"]), 2)
        self.assertEqual(result["attempts"][0]["status"], "prior_cash_not_released")
        first, second = result["chain"]
        self.assertEqual(second["cash_before_entry"], first["cash_after_entry"]+first["payout"])
        self.assertNotEqual(first["shares"], second["shares"])
        self.assertGreaterEqual(pd.Timestamp(second["entry_ts"]), pd.Timestamp(first["cash_release_ts"]))
        self.assertFalse(result["first_ten_passed"])

    def test_bad_callback_is_rejected_before_or_after_selection(self):
        with self.assertRaises(ValueError):
            replay([], Source([]), resolver=object())
        with self.assertRaises(ValueError):
            replay([candidate()], Source([(104, 60, 10000), (117, 60, 1)]),
                   resolver=lambda *args: None)


if __name__ == "__main__":
    unittest.main()
