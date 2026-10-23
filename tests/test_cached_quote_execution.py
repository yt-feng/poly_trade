from copy import deepcopy
import hashlib
import unittest

import numpy as np
import pandas as pd

from analysis import cached_quote_execution as cached
from analysis import timed_quote_execution as reference


START = pd.Timestamp("2026-01-01T00:00:00Z")
PROTOCOL = {"entry_delay_seconds": 4., "latency_seconds": 8., "hold_seconds": 30.,
    "max_quote_delay_seconds": 3., "edge_floor": .02, "fee_rate": .07, "slippage": .01,
    "depth_haircut": .1, "depth_thresholds": [3., 5., 7.],
    "cash_release_delay_after_close_seconds": 900., "initial_cash": 10.,
    "minimum_order_usd": 5., "minimum_order_shares": 5., "required_fills": 10,
    "run_order": ["train", "run"], "minimum_training_runs": 1, "evaluation_block_runs": 1}


def candidate(number=0, *, shift=0, prediction=.8, side="up"):
    begin = START + pd.Timedelta(seconds=shift)
    return {"market_id": f"run::market-{number}", "run_name": "run", "slug": f"market-{number}",
        "side": side, "decision_ts": begin + pd.Timedelta(seconds=120),
        "market_close_ts": begin + pd.Timedelta(seconds=300),
        "decision_ask_price": .5, "decision_sell_size": 1000.,
        "predicted_exit_bid_price": prediction}


def book(seconds=(132, 170, 171), *, shift=0, bids=None, asks=None, sizes=None, conflicts=None):
    count = len(seconds)
    return pd.DataFrame({"ts_utc": [START + pd.Timedelta(seconds=shift+s) for s in seconds],
        "buy_up_cents": asks if asks is not None else [50.] * count,
        "buy_down_cents": asks if asks is not None else [50.] * count,
        "sell_up_cents": bids if bids is not None else [70.] * count,
        "sell_down_cents": bids if bids is not None else [70.] * count,
        "buy_up_size": sizes if sizes is not None else [10000.] * count,
        "sell_up_size": sizes if sizes is not None else [10000.] * count,
        "buy_down_size": sizes if sizes is not None else [10000.] * count,
        "sell_down_size": sizes if sizes is not None else [10000.] * count,
        "quote_conflict": conflicts if conflicts is not None else [False] * count})


def selected_record(row, quotes, protocol=PROTOCOL):
    """Build a synthetic saved record from the actual reference selector output."""
    result = dict.fromkeys(cached.CACHE_FIELDS)
    result.update({field: row[field] for field in cached.IDENTITY_FIELDS})
    result["entry_target_ts"] = row["decision_ts"] + pd.Timedelta(
        seconds=protocol["entry_delay_seconds"]+protocol["latency_seconds"])
    result["exit_quote_status"] = cached.NOT_REACHED
    for kind in ("entry", "exit"):
        target = result[f"{kind}_target_ts"]
        picked, reason = reference.select_book_quote(quotes, target, row["market_close_ts"],
            side=row["side"], kind=kind, max_quote_delay_seconds=protocol["max_quote_delay_seconds"])
        result[f"{kind}_quote_status"] = reason
        if picked is None:
            break
        result[f"{kind}_quote_ts"] = picked["ts_utc"]
        for field in cached.BOOK_FIELDS:
            value = picked.get(field)
            result[f"{kind}_{field}"] = float(value) if value is not None and pd.notna(value) else None
        if kind == "entry":
            result["exit_target_ts"] = picked["ts_utc"] + pd.Timedelta(
                seconds=protocol["hold_seconds"]+protocol["latency_seconds"])
    return result


def source_for(rows, books, protocol=PROTOCOL):
    return cached.CachedQuoteSource(pd.DataFrame(
        [selected_record(row, books.get(row["market_id"]), protocol) for row in rows],
        columns=cached.CACHE_FIELDS), protocol)


def replay(rows, books, *, caps=None, **kwargs):
    source = source_for(rows, books)
    return cached.replay_cached_quotes(pd.DataFrame(rows), ["run"], source,
        capacity_cap_by_id=caps, **kwargs)


class CachedQuoteSourceTests(unittest.TestCase):
    def assert_same_replay(self, rows, books, **kwargs):
        expected = reference.replay_timed_quotes(pd.DataFrame(rows), ["run"], books, **kwargs)
        actual = replay(rows, books, **kwargs)
        self.assertEqual(actual, expected)
        return actual

    def test_all_unavailable_reasons_preserved_without_synthetic_ticks(self):
        row = candidate()
        cases = [None, book((140,)), book(asks=[np.nan]*3), book(conflicts=[True, False, False])]
        expected = ["missing_book", "no_quote_in_window", "invalid_quote_price", "unknown_quote_conflict"]
        for quotes, reason in zip(cases, expected):
            with self.subTest(reason=reason):
                record = selected_record(row, quotes)
                source = cached.CachedQuoteSource(pd.DataFrame([record]), PROTOCOL)
                picked, status = source.select(row["market_id"], record["entry_target_ts"],
                    row["market_close_ts"], side="up")
                self.assertIsNone(picked)
                self.assertEqual(status, reason)
                result = self.assert_same_replay([row], {row["market_id"]: quotes})
                self.assertEqual(result["attempts"][0]["reason"], reason)
        row["market_close_ts"] = START + pd.Timedelta(seconds=132)
        result = self.assert_same_replay([row], {row["market_id"]: book()})
        self.assertEqual(result["attempts"][0]["reason"], "target_at_or_after_close")

    def test_exit_unavailable_reasons_and_known_entry_keep_inventory(self):
        for reason in cached.UNAVAILABLE_REASONS:
            with self.subTest(reason=reason):
                row = candidate()
                if reason == "target_at_or_after_close":
                    row["market_close_ts"] = START + pd.Timedelta(seconds=170)
                record = selected_record(row, book((132,)))
                record["exit_quote_status"] = reason
                source = cached.CachedQuoteSource(pd.DataFrame([record]), PROTOCOL)
                picked, actual = source.select(row["market_id"], record["exit_target_ts"],
                    row["market_close_ts"], side="up", kind="exit")
                self.assertIsNone(picked)
                self.assertEqual(actual, reason)
                result = cached.replay_cached_quotes(pd.DataFrame([row]), ["run"], source)
                self.assertEqual(result["fills"], 1)
                self.assertEqual(result["resolved_fills"], 0)
                self.assertGreater(result["locked_shares"], 0)
                self.assertEqual(result["chain"][0]["unresolved_reason"], reason)
                self.assertIsNone(result["chain"][0]["payout"])

    def test_actual_entry_clock_anchors_exit_and_first_price_not_depth(self):
        row = candidate()
        quotes = book((132, 133, 170, 171, 172), asks=[np.nan, 50., 50., 50., 50.],
                      bids=[50., 50., 99., 45., 99.])
        result = self.assert_same_replay([row], {row["market_id"]: quotes})
        self.assertEqual(result["chain"][0]["entry_ts"], "2026-01-01T00:02:13+00:00")
        self.assertEqual(result["chain"][0]["exit_target_ts"], "2026-01-01T00:02:51+00:00")
        self.assertLess(result["chain"][0]["realized_pnl"], 0)
        quotes = book((132, 170, 171), sizes=[10000., 1., 10000.])
        result = self.assert_same_replay([row], {row["market_id"]: quotes})
        self.assertEqual(result["chain"][0]["exit_quote_evidence"]["available_shares"], 1.)
        self.assertEqual(result["resolved_fills"], 0)

    def test_missing_other_side_fields_cannot_gate_original_side_execution(self):
        class ForbiddenQuality:
            def __bool__(self):
                raise AssertionError("future quality must not be inspected")
        for side in ("up", "down"):
            row = candidate(side=side)
            quotes = book()
            other = "down" if side == "up" else "up"
            quotes[f"buy_{other}_cents"] = np.nan
            quotes[f"sell_{other}_size"] = -3.
            record = selected_record(row, quotes)
            record.update(simulation_cash_domain_observed=ForbiddenQuality(),
                paired_high_quality_quotes=ForbiddenQuality(), minimum_order_has_pair_capacity=ForbiddenQuality())
            source = cached.CachedQuoteSource(pd.DataFrame([record]), PROTOCOL)
            result = cached.replay_cached_quotes(pd.DataFrame([row]), ["run"], source)
            expected = reference.replay_timed_quotes(pd.DataFrame([row]), ["run"], {row["market_id"]: quotes})
            self.assertEqual(result, expected)
            self.assertEqual(result["resolved_fills"], 1)

    def test_zero_and_missing_same_side_size_are_not_replaced_by_future_size(self):
        for sizes in ([0., 10000., 10000.], [np.nan, 10000., 10000.],
                      [10000., 0., 10000.], [10000., np.nan, 10000.]):
            with self.subTest(sizes=sizes):
                row = candidate()
                result = self.assert_same_replay([row], {row["market_id"]: book(sizes=sizes)})
                self.assertEqual(result["resolved_fills"], 0)

    def test_request_boundaries_mutation_isolation_and_bad_cache_rejected(self):
        row = candidate()
        record = selected_record(row, book())
        source = cached.CachedQuoteSource(pd.DataFrame([record]), PROTOCOL)
        args = (row["market_id"], record["entry_target_ts"], row["market_close_ts"])
        picked, _ = source.select(*args, side="up")
        picked["buy_up_size"] = 0
        again, _ = source.select(*args, side="up")
        self.assertEqual(again["buy_up_size"], 10000.)
        requests = [(("missing", *args[1:]), {"side": "up"}),
            ((args[0], args[1]+pd.Timedelta(seconds=1), args[2]), {"side": "up"}),
            ((args[0], args[1], args[2]+pd.Timedelta(seconds=1)), {"side": "up"}),
            (args, {"side": "down"}), (args, {"side": "up", "kind": "other"}),
            (args, {"side": "up", "max_quote_delay_seconds": 2})]
        for pos, kw in requests:
            with self.subTest(pos=pos, kw=kw), self.assertRaises(ValueError):
                source.select(*pos, **kw)
        mutations = [dict(entry_target_ts=record["entry_target_ts"]+pd.Timedelta(seconds=1)),
            dict(exit_target_ts=record["exit_target_ts"]+pd.Timedelta(seconds=1)),
            dict(entry_quote_ts=record["entry_target_ts"]+pd.Timedelta(seconds=4)),
            dict(entry_buy_up_cents=0.), dict(entry_buy_down_size=float("inf")),
            dict(entry_buy_down_size=True), dict(entry_quote_status="invented"),
            dict(market_id="other"), dict(exit_quote_status=cached.NOT_REACHED)]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                cached.CachedQuoteSource(pd.DataFrame([{**record, **mutation}]), PROTOCOL)
        with self.assertRaises(ValueError):
            cached.CachedQuoteSource(pd.DataFrame([record, record]), PROTOCOL)
        with self.assertRaises(ValueError):
            cached.CachedQuoteSource(pd.DataFrame([record]).drop(columns=["entry_quote_ts"]), PROTOCOL)

    def test_decision_and_arrival_rejections_do_not_read_later_selections(self):
        class RejectReads:
            def select(self, *args, **kwargs):
                raise AssertionError("decision must reject before reading saved quotes")
        rejected = candidate(prediction=.55)
        result = cached.replay_cached_quotes(pd.DataFrame([rejected]), ["run"], RejectReads())
        self.assertEqual(result["attempts"][0]["status"], "decision_cost_floor_not_met")
        class NoExitRead:
            def __init__(self, source):
                self.source = source
            def select(self, *args, **kwargs):
                if kwargs["kind"] == "exit":
                    raise AssertionError("arrival cancellation cannot inspect exit")
                return self.source.select(*args, **kwargs)
        row = candidate()
        records = []
        for bid in (1., 99.):
            quotes = book(asks=[90., 50., 50.], bids=[70., bid, bid])
            source = NoExitRead(source_for([row], {row["market_id"]: quotes}))
            records.append(cached.replay_cached_quotes(pd.DataFrame([row]), ["run"], source))
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0]["attempts"][0]["status"], "entry_cost_limit_exceeded")

    def test_legacy_losses_pending_release_ten_entries_and_unknown_tenth_equal(self):
        rows = [candidate(0), candidate(1, shift=500), candidate(2, shift=1300)]
        books = {row["market_id"]: book(shift=shift, bids=[45.]*3)
                 for row, shift in zip(rows, (0, 500, 1300))}
        result = self.assert_same_replay(rows, books)
        self.assertEqual(result["attempts"][0]["status"], "prior_cash_not_released")
        self.assertLess(result["ending_cash"], 10.)
        rows = [candidate(i, shift=1300*i) for i in range(11)]
        books = {row["market_id"]: book(shift=1300*i) for i, row in enumerate(rows)}
        result = self.assert_same_replay(rows, books)
        self.assertTrue(result["first_ten_passed"])
        self.assertEqual(result["fills"], 10)
        books[rows[9]["market_id"]] = book((132,), shift=1300*9)
        result = self.assert_same_replay(rows, books)
        self.assertEqual(result["resolved_fills"], 9)
        self.assertFalse(result["ten_resolved_sequence_complete"])
        self.assertGreater(result["locked_shares"], 0)

    def test_scope_wrappers_exactly_reproduce_original(self):
        row = candidate()
        quotes = {row["market_id"]: book()}
        source = source_for([row], quotes)
        actual = cached.replay_arm(pd.DataFrame([row]), source, PROTOCOL)
        expected = cached.parent_audit.replay_arm(pd.DataFrame([row]), quotes, PROTOCOL)
        self.assertEqual(actual, expected)
        self.assertEqual([cached.parent_audit.result_fingerprint(x) for x in actual],
                         [cached.parent_audit.result_fingerprint(x) for x in expected])


class ForecastCapacityExecutionTests(unittest.TestCase):
    def test_nonbinding_cap_preserves_every_legacy_economic_field(self):
        rows = [candidate(0), candidate(1, shift=500), candidate(2, shift=1300)]
        books = {row["market_id"]: book(shift=shift, bids=[45.]*3)
                 for row, shift in zip(rows, (0, 500, 1300))}
        expected = replay(rows, books)
        actual = replay(rows, books, caps={row["market_id"]: 10000. for row in rows})
        actual["sizing_basis"] = expected["sizing_basis"]
        for event in actual["chain"]:
            event.pop("quantity_cap_evidence")
        self.assertEqual(actual, expected)

    def test_cap_shrinks_without_using_actual_exit_depth_and_can_prevent_lock(self):
        row = candidate()
        quotes = {row["market_id"]: book(sizes=[10000., 150., 10000.])}
        original = replay([row], quotes)
        capped = replay([row], quotes, caps={row["market_id"]: 12.})
        self.assertEqual(original["resolved_fills"], 0)
        self.assertEqual(capped["resolved_fills"], 1)
        event = capped["chain"][0]
        self.assertEqual(event["shares"], 12.)
        self.assertGreater(event["quantity_cap_evidence"]["legacy_shares"], 12.)
        self.assertEqual(event["quantity_cap_evidence"]["forecast_exit_capacity_shares"], 12.)
        self.assertAlmostEqual(event["entry_cost"], 12*.51)
        self.assertAlmostEqual(event["entry_fee"], 12*.07*.51*.49)
        self.assertAlmostEqual(event["payout"], 12*(.69-.07*.69*.31))
        insufficient = {row["market_id"]: book(sizes=[10000., 100., 10000.])}
        locked = replay([row], insufficient, caps={row["market_id"]: 12.})
        self.assertEqual(locked["chain"][0]["shares"], 12.)
        self.assertEqual(locked["locked_shares"], 12.)
        self.assertEqual(locked["resolved_fills"], 0)

    def test_cap_below_minimum_skips_with_unchanged_cash_and_continues(self):
        rows = [candidate(0), candidate(1, shift=1300)]
        books = {row["market_id"]: book(shift=1300*i) for i, row in enumerate(rows)}
        for cap in (0., 4.99, 9.):
            with self.subTest(cap=cap):
                result = replay(rows, books, caps={rows[0]["market_id"]: cap, rows[1]["market_id"]: 12.})
                self.assertEqual(result["attempts"][0]["status"], "forecast_exit_capacity_below_minimum_order")
                self.assertEqual(result["attempts"][0]["cash_unchanged"], 10.)
                self.assertEqual(result["fills"], 1)
                self.assertEqual(result["chain"][0]["market_id"], rows[1]["market_id"])
                self.assertEqual(result["chain"][0]["cash_before_entry"], 10.)

    def test_original_cash_floor_stops_before_cap_abstention(self):
        rows = [candidate(0), candidate(1, shift=1300), candidate(2, shift=2600)]
        books = {row["market_id"]: book(shift=1300*i, bids=[20.]*3) for i, row in enumerate(rows)}
        caps = {rows[0]["market_id"]: 100., rows[1]["market_id"]: 0., rows[2]["market_id"]: 100.}
        result = replay(rows, books, caps=caps)
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["failure_reason"], "cash_cannot_fit_minimum_order")
        self.assertEqual(result["attempts"][0]["status"], "cash_cannot_fit_minimum_order")
        self.assertLess(result["ending_cash"], 5.)

    def test_actual_entry_depth_does_not_shrink_cap_or_search_next_tick(self):
        row = candidate()
        quotes = {row["market_id"]: book((132, 133, 170), sizes=[100., 10000., 10000.])}
        result = replay([row], quotes, caps={row["market_id"]: 12.})
        self.assertEqual(result["fills"], 0)
        self.assertEqual(result["attempts"][0]["status"], "entry_depth_insufficient_or_invalid")
        self.assertEqual(result["ending_cash"], 10.)

    def test_pending_cash_order_unchanged_and_capacity_map_copied_before_quote_access(self):
        rows = [candidate(0), candidate(1, shift=500)]
        books = {row["market_id"]: book(shift=500*i) for i, row in enumerate(rows)}
        caps = {rows[0]["market_id"]: 12., rows[1]["market_id"]: 0.}
        source = source_for(rows, books)
        class MutatingSource:
            def select(self, *args, **kwargs):
                caps[rows[0]["market_id"]] = 10000.
                return source.select(*args, **kwargs)
        result = cached.replay_cached_quotes(pd.DataFrame(rows), ["run"], MutatingSource(), capacity_cap_by_id=caps)
        self.assertEqual(result["chain"][0]["shares"], 12.)
        self.assertEqual(result["attempts"][0]["status"], "prior_cash_not_released")

    def test_invalid_or_absent_forecast_fails_instead_of_deleting_candidates(self):
        row = candidate()
        books = {row["market_id"]: book()}
        for caps in ({}, {row["market_id"]: np.nan}, {row["market_id"]: float("inf")},
                     {row["market_id"]: -1.}, {row["market_id"]: True}):
            with self.subTest(caps=caps), self.assertRaises(ValueError):
                replay([row], books, caps=caps)


class CachedControlRegenerationTests(unittest.TestCase):
    def fixture(self):
        rows = [candidate(0), candidate(1, shift=1300, prediction=.55)]
        frame = pd.DataFrame(rows)
        predictions = frame[["market_id", "run_name", "predicted_exit_bid_price"]].copy()
        features = frame.drop(columns=["predicted_exit_bid_price"])
        admitted = [{name: str(rows[0][name]) for name in cached.parent_audit.CANDIDATE_FIELDS}]
        config = {"base_protocol": PROTOCOL,
            "frozen_control_candidate_fields": cached.parent_audit.CANDIDATE_FIELDS,
            "expected_control_candidate_count": 1, "frozen_control_candidates": admitted,
            "frozen_control_candidate_rows_sha256": hashlib.sha256(cached.parent_audit.canonical(admitted)).hexdigest()}
        return features, predictions, config

    def test_full_projection_regenerates_cost_intent_and_keeps_unknown_label(self):
        features, predictions, config = self.fixture()
        features["future_bid_price"] = [np.nan, .7]
        actual, decisions = cached.regenerate_control(features, predictions, config)
        self.assertEqual(actual["market_id"].tolist(), ["run::market-0"])
        self.assertEqual(len(decisions), 2)
        self.assertTrue(pd.isna(actual.iloc[0]["future_bid_price"]))
        self.assertEqual(decisions["decision_signal_admitted"].tolist(), [True, False])

    def test_fit_run_projection_is_bound_to_the_opportunity_run(self):
        features, predictions, config = self.fixture()
        predictions["fit_run"] = predictions["run_name"]
        actual, _ = cached.regenerate_control(features, predictions, config)
        self.assertEqual(actual["fit_run"].tolist(), ["run"])
        predictions.loc[0, "fit_run"] = "different"
        with self.assertRaisesRegex(ValueError, "control_prediction_fit_run_mismatch"):
            cached.regenerate_control(features, predictions, config)

    def test_mismatch_missing_duplicate_and_reordered_scores_fail(self):
        features, predictions, config = self.fixture()
        for altered in (predictions.iloc[::-1], predictions.iloc[:1],
                        pd.concat([predictions, predictions.iloc[:1]], ignore_index=True),
                        predictions.assign(predicted_exit_bid_price=[.9, .55])):
            with self.subTest(altered=altered.to_dict("records")), self.assertRaises(ValueError):
                cached.regenerate_control(features, altered, config)
        modified = deepcopy(config)
        modified["frozen_control_candidate_rows_sha256"] = "0"*64
        with self.assertRaises(ValueError):
            cached.regenerate_control(features, predictions, modified)
        with self.assertRaises(ValueError):
            cached.regenerate_control(features, predictions.assign(future_quality=True), config)


if __name__ == "__main__":
    unittest.main()
