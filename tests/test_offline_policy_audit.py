import unittest

import numpy as np
import pandas as pd

from analysis import offline_policy_audit as audit


class OfflinePolicyAuditTests(unittest.TestCase):
    def _signal_fixture(self, probability):
        class FixedProbability:
            def predict_proba(self, frame):
                return np.tile([1 - probability, probability], (len(frame), 1))

        row = {name: 0.0 for name in audit.signal.FEATURES}
        row.update(buy_up_price_2m=.79, buy_down_price_2m=.21,
                   buy_up_size_2m=1000, buy_down_size_2m=1000,
                   spread_up_median_first2m=.02, spread_down_median_first2m=.02,
                   overround_median_first2m=0,
                   first_quote_ts=pd.Timestamp("2026-01-01T00:00:00Z"),
                   slug="btc-updown-5m-1767225600")
        return FixedProbability(), pd.DataFrame([row])

    def test_fill_cost_gate_rejects_edge_that_only_passes_without_slippage(self):
        model, frame = self._signal_fixture(.8213)
        legacy = audit.signal._candidate_frame(model, pd.Series(dtype=float), frame, .01)
        self.assertEqual(len(legacy), 1)
        self.assertAlmostEqual(legacy.iloc[0]["edge"], .0201)
        checked = audit.cost_checked_candidate_frame(model, pd.Series(dtype=float), frame, .01)
        self.assertTrue(checked.empty)

    def test_larger_slippage_cannot_improve_net_edge_or_restore_rejected_candidate(self):
        model, frame = self._signal_fixture(.83)
        selected = []
        previous_edge = float("inf")
        for slippage in (0, .005, .01, .015):
            checked = audit.cost_checked_candidate_frame(model, pd.Series(dtype=float), frame, slippage)
            selected.append(len(checked))
            if not checked.empty:
                self.assertLess(checked.iloc[0]["edge"], previous_edge)
                previous_edge = checked.iloc[0]["edge"]
            legacy = audit.signal._candidate_frame(model, pd.Series(dtype=float), frame, slippage)
            self.assertTrue(set(checked["slug"]).issubset(set(legacy["slug"])))
        self.assertEqual(selected, [1, 1, 0, 0])

    def test_zero_slippage_preserves_legacy_candidate_frame(self):
        model, frame = self._signal_fixture(.83)
        legacy = audit.signal._candidate_frame(model, pd.Series(dtype=float), frame, 0)
        checked = audit.cost_checked_candidate_frame(model, pd.Series(dtype=float), frame, 0)
        pd.testing.assert_frame_equal(checked, legacy)

    def test_cost_gate_rejects_negative_or_nonfinite_slippage(self):
        model, frame = self._signal_fixture(.83)
        for slippage in (-.001, float("nan"), float("inf")):
            with self.subTest(slippage=slippage), self.assertRaisesRegex(ValueError, "invalid_nonnegative_slippage"):
                audit.cost_checked_candidate_frame(model, pd.Series(dtype=float), frame, slippage)

    def test_unavailable_registered_fields_are_diagnosed_without_imputation(self):
        values = {name: 1.0 for name in audit.source.LOGIT_FEATURES}
        values.update(prefix_quality_passed=True, trade_count_sum_first2m=float("nan"),
                      trade_volume_sum_first2m=float("nan"), gate_target_visible_before_decision=True)
        frame = pd.DataFrame([values])
        allowed = [name for name in audit.source.LOGIT_FEATURES if audit.feature_available_seconds(name) <= 120]
        rejected, report = audit.admit_feature_rows(frame, allowed)
        self.assertTrue(rejected.empty)
        self.assertEqual(report["prefix_quality_passed_rows"], 1)
        self.assertEqual(report["prefix_pass_but_missing_registered_inputs_rows"], 1)
        self.assertEqual(report["nonfinite_rows_by_required_field"]["trade_count_sum_first2m"], 1)
        revised = [name for name in allowed if name not in ("trade_count_sum_first2m", "trade_volume_sum_first2m")]
        admitted, _ = audit.admit_feature_rows(frame, revised)
        self.assertEqual(len(admitted), 1)
        self.assertTrue(pd.isna(admitted.iloc[0]["trade_count_sum_first2m"]))

    def test_empty_dataset_and_unfitted_run_are_not_evaluations(self):
        self.assertEqual(audit.evaluation_status(0, 0), ("not_evaluated_empty_quality_dataset", False))
        self.assertEqual(audit.evaluation_status(50, 0), ("not_evaluated_insufficient_training_data", False))
        self.assertEqual(audit.evaluation_status(200, 1), ("evaluated", True))

    def _entry_fixture(self, outcome=float("nan")):
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        row = {"market_id": "a::market", "run_name": "a", "slug": "market", "side": "up",
            "entry_ts": start + pd.Timedelta(seconds=124), "market_close_ts": start + pd.Timedelta(seconds=300),
            "slippage": 0.005, "fill_price": 0.505, "sell_up_size_2m": 1000,
            "sell_down_size_2m": 1000, "outcome_up": outcome,
            "label_observed_ts": start + pd.Timedelta(seconds=300) if pd.notna(outcome) else pd.NaT}
        quotes = pd.DataFrame({"ts_utc": [start + pd.Timedelta(seconds=x) for x in (124, 128, 240, 299)],
            "buy_up_cents": [50, 60, 60, 60], "buy_up_size": [1000] * 4,
            "sell_up_cents": [20] * 4, "sell_up_size": [1000] * 4})
        return pd.DataFrame([row]), {"a::market": quotes}

    def test_no_future_exit_keeps_debit_and_unresolved_position(self):
        candidates, quotes = self._entry_fixture()
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=0,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(result["fills"], 1)
        self.assertLess(result["ending_cash"], 0.00001)
        self.assertGreater(result["locked_shares"], 0)
        self.assertEqual(result["chain"][0]["status"], "unresolved_position")

    def test_no_exit_terminal_loss_remains_on_ledger(self):
        candidates, quotes = self._entry_fixture(outcome=0)
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=0,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["chain"][0]["payout"], 0)
        self.assertFalse(result["first_ten_passed"])
        self.assertTrue(result["ending_cash_available_ts"].endswith("00:20:00+00:00"))

    def test_entry_and_terminal_evidence_do_not_change_replay_decisions(self):
        candidates, quotes = self._entry_fixture(outcome=0)
        baseline = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=.5, thresholds=(3, 5, 7))
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        candidates["q_side"] = .85
        candidates["decision_ts"] = start + pd.Timedelta(seconds=120)
        candidates["terminal_proxy_observed_ts"] = start + pd.Timedelta(seconds=299)
        candidates["terminal_proxy_gap_seconds"] = -1
        reported = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=.5, thresholds=(3, 5, 7))
        for key in ("fills", "ending_cash", "first_ten_passed", "failure_reason", "ending_cash_available_ts"):
            self.assertEqual(reported[key], baseline[key])
        event = reported["chain"][0]
        for key in ("entry_ts", "entry_fill_price", "shares", "entry_cost", "entry_fee", "cash_after_entry", "status", "payout", "cash_release_ts"):
            self.assertEqual(event[key], baseline["chain"][0][key])
        self.assertEqual(event["side"], "up")
        self.assertEqual(event["q_side"], .85)
        self.assertEqual(event["decision_ts"], "2026-01-01T00:02:00+00:00")
        entry = event["entry_quote_evidence"]
        self.assertEqual(entry["observed_ts"], event["entry_ts"])
        self.assertAlmostEqual(entry["ask_price"] + entry["slippage"], event["entry_fill_price"])
        self.assertEqual(entry["effective_available_shares"], entry["available_shares"] * entry["depth_haircut"])
        self.assertGreaterEqual(entry["effective_available_shares"], event["shares"])
        terminal = event["terminal_proxy_evidence"]
        self.assertEqual(terminal["gap_seconds"], -1)
        self.assertEqual(pd.Timestamp(terminal["observed_ts"]) - pd.Timestamp(terminal["market_close_ts"]), pd.Timedelta(seconds=-1))
        self.assertEqual(terminal["label_available_ts"], terminal["market_close_ts"])
        self.assertIsNone(baseline["chain"][0]["q_side"])
        self.assertIsNone(baseline["chain"][0]["decision_ts"])

    def test_delayed_exit_evidence_verifies_trigger_limit_depth_and_cash(self):
        candidates, quotes = self._entry_fixture()
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        quotes["a::market"] = pd.DataFrame({
            "ts_utc": [start + pd.Timedelta(seconds=s) for s in (128, 140, 144, 150, 154)],
            "buy_up_cents": [60] * 5, "buy_up_size": [1000] * 5,
            "sell_up_cents": [20, 80, 40, 80, 75], "sell_up_size": [1000] * 5,
        })
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=.5, thresholds=(3, 5, 7))
        event = result["chain"][0]
        evidence = event["exit_quote_evidence"]
        self.assertEqual(event["status"], "quote_exit")
        # The first trigger's delayed book misses the limit. The recorded
        # trigger belongs to the later successful attempt, not that cancellation.
        self.assertEqual(evidence["trigger_observed_ts"], "2026-01-01T00:02:30+00:00")
        self.assertEqual(evidence["arrival_observed_ts"], "2026-01-01T00:02:34+00:00")
        self.assertEqual(pd.Timestamp(evidence["intended_arrival_ts"]) - pd.Timestamp(evidence["trigger_observed_ts"]), pd.Timedelta(seconds=4))
        self.assertGreaterEqual(pd.Timestamp(evidence["arrival_observed_ts"]), pd.Timestamp(evidence["intended_arrival_ts"]))
        self.assertEqual(evidence["arrival_observed_ts"], event["exit_ts"])
        for prefix in ("trigger", "arrival"):
            self.assertGreaterEqual(evidence[prefix + "_effective_available_shares"], event["shares"])
            self.assertGreaterEqual(evidence[prefix + "_net_proceeds"], evidence["minimum_net_proceeds"])
        self.assertAlmostEqual(evidence["arrival_bid_price"] - evidence["slippage"], event["exit_fill_price"])
        self.assertAlmostEqual(evidence["minimum_net_proceeds"], event["entry_cost"] + event["entry_fee"])
        self.assertEqual(evidence["arrival_net_proceeds"], event["payout"])
        self.assertAlmostEqual(result["ending_cash"], event["cash_after_entry"] + event["payout"])

    def test_released_cash_clock_survives_next_minimum_order_failure(self):
        candidates, quotes = self._entry_fixture(outcome=0)
        following = candidates.iloc[0].copy()
        following["market_id"] = "a::following"
        following["slug"] = "following"
        following["entry_ts"] += pd.Timedelta(seconds=1300)
        following["market_close_ts"] += pd.Timedelta(seconds=1300)
        following["label_observed_ts"] += pd.Timedelta(seconds=1300)
        candidates = pd.concat([candidates, following.to_frame().T], ignore_index=True)
        quotes["a::following"] = quotes["a::market"].copy()
        quotes["a::following"]["ts_utc"] += pd.Timedelta(seconds=1300)
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=0,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["failure_reason"], "cash_fraction_cannot_fit_minimum_order")
        self.assertAlmostEqual(result["ending_cash"], 0)
        self.assertEqual(result["ending_cash_available_ts"], result["chain"][0]["cash_release_ts"])
        self.assertNotEqual(result["ending_cash_available_ts"], result["chain"][0]["entry_ts"])

    def test_latency_uses_delayed_entry_price_and_depth(self):
        candidates, quotes = self._entry_fixture()
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertAlmostEqual(result["chain"][0]["entry_fill_price"], .605)
        quotes["a::market"].loc[1, "buy_up_size"] = 1
        missing = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(missing["fills"], 0)
        self.assertEqual(missing["ending_cash"], 10)

    def test_availability_rejects_later_and_unknown_observations(self):
        self.assertEqual(audit.feature_available_seconds("btc_move_2m"), 120)
        self.assertEqual(audit.feature_available_seconds("btc_move_4m"), 240)
        with self.assertRaises(ValueError):
            audit.feature_available_seconds("unregistered_future_statistic")

    def test_training_uses_only_labels_known_before_decision_and_other_markets(self):
        rows = pd.DataFrame({"run_name": ["a"] * 4, "slug": ["old", "late", "same", "equal"],
            "label_observed_ts": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:03:00Z",
                                                "2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z"])})
        current = pd.DataFrame({"slug": ["same"], "decision_ts": pd.to_datetime(["2026-01-01T00:02:00Z"])})
        self.assertEqual(audit.causal_training_rows(rows, ["a"], current)["slug"].tolist(), ["old"])

    def test_blocks_are_disjoint_include_final_partial_block(self):
        runs = [str(x) for x in range(29)]
        blocks = audit.disjoint_blocks(runs, 12)
        self.assertEqual([len(b) for b in blocks], [12, 12, 5])
        self.assertEqual([r for b in blocks for r in b], runs)
        with self.assertRaises(ValueError):
            audit.disjoint_blocks(["same", "same"], 1)

    def test_signal_contract_restored_after_exception(self):
        original = audit.signal.FEATURES
        entry = audit.signal.ENTRY_SECONDS_AFTER_START
        with self.assertRaises(RuntimeError):
            with audit.isolated_signal_contract(["btc_move_1m"], 130):
                raise RuntimeError("test")
        self.assertIs(audit.signal.FEATURES, original)
        self.assertEqual(audit.signal.ENTRY_SECONDS_AFTER_START, entry)

    def test_post_decision_changes_do_not_change_quality_or_allowed_features(self):
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        rows = []
        for second in (0, 60, 120, 180, 240, 299):
            row = {key: 100.0 for key in audit.source.REQUIRED_NUMERIC}
            row.update(ts_iso=(start + pd.Timedelta(seconds=second)).isoformat(), slug="btc-updown-5m-1767225600",
                run_name="a", source_file="fixture", target_price=1000.0, final_price=1000 + second / 60,
                buy_up_cents=60, sell_up_cents=58, buy_down_cents=42, sell_down_cents=40,
                mid_up_cents=59, mid_down_cents=41, spread_up_cents=2, spread_down_cents=2)
            rows.append(row)
        original = pd.DataFrame(rows)
        changed = original.copy()
        changed.loc[4, "final_price"] = 1100.0
        changed.loc[4, "buy_up_cents"] = float("nan")
        qa, qb = (audit.source.prepare_quotes(frame) for frame in (original, changed))
        self.assertTrue(audit.prefix_quality(qa, 90).iloc[0]["prefix_quality_passed"])
        self.assertTrue(audit.prefix_quality(qb, 90).iloc[0]["prefix_quality_passed"])
        fa, fb = (audit.source.build_features(q, strict_quality=False) for q in (qa, qb))
        allowed = [name for name in audit.source.LOGIT_FEATURES if audit.feature_available_seconds(name) <= 120]
        pd.testing.assert_frame_equal(fa[allowed], fb[allowed])
        self.assertNotEqual(fa.iloc[0]["btc_move_4m"], fb.iloc[0]["btc_move_4m"])
        truncated = audit.prefix_quality(qa[qa["ts_utc"] <= start + pd.Timedelta(seconds=120)], 90)
        self.assertTrue(truncated.iloc[0]["prefix_quality_passed"])
        self.assertTrue(pd.isna(truncated.iloc[0]["qualified_outcome_up"]))
        near = audit.prefix_quality(qa, 90, terminal_gap_seconds=1)
        self.assertEqual(near.iloc[0]["terminal_proxy_gap_seconds"], -1)
        self.assertEqual(near.iloc[0]["label_observed_ts"], start + pd.Timedelta(seconds=300))
        strict = audit.prefix_quality(qa, 90, terminal_gap_seconds=0)
        self.assertTrue(pd.isna(strict.iloc[0]["qualified_outcome_up"]))


if __name__ == "__main__":
    unittest.main()
