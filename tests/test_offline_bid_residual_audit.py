import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis import offline_bid_residual_audit as audit


def config_fixture(runs=None):
    names = runs or ["old", "new"]
    return {"protocol_id": "synthetic", "evaluation_mode": "book_bid_residual",
        "source_commit": "a" * 40, "expected_code_commit": "b" * 40,
        "features": audit.FEATURES.copy(), "source_runs": names, "run_order": names,
        "expected_files_per_run": 1, "minimum_prefix_quotes": 3, "minimum_training_runs": 1,
        "minimum_training_labels": 100, "evaluation_block_runs": 12, "max_source_rows": 10000,
        "max_feature_rows": 1000, "max_scored_runs": 10, "max_replay_cells": 16,
        "opening_tolerance_seconds": 3., "maximum_snapshot_age_seconds": 3.,
        "decision_cutoff_seconds": 120., "entry_delay_seconds": 4., "hold_seconds": 30.,
        "latency_seconds": 8., "max_quote_delay_seconds": 3., "edge_floor": .02,
        "fee_rate": .07, "slippage": .01, "depth_haircut": .1,
        "minimum_order_usd": 5., "minimum_order_shares": 5., "depth_thresholds": [3., 5., 7.],
        "cash_release_delay_after_close_seconds": 900., "ridge_alpha": 1., "ridge_solver": "svd",
        "initial_cash": 10., "required_fills": 10}


def raw_fixture():
    start = pd.Timestamp("2026-01-02T00:00:00Z")
    offsets = [0, 60, 120, 134, 170, 172, 174]
    frame = pd.DataFrame({"ts_iso": [start + pd.Timedelta(seconds=n) for n in offsets],
        "slug": "btc-updown-5m-" + str(int(start.timestamp())), "source_file": "a.csv"})
    for action in ("buy", "sell"):
        for side in ("up", "down"):
            frame[f"{action}_{side}_cents"] = (60. if action == "buy" else 58.) if side == "up" else (45. if action == "buy" else 43.)
            frame[f"{action}_{side}_size"] = 1000.
    frame.loc[frame.index[4], "sell_up_cents"] = 90.
    frame.loc[frame.index[5], "sell_up_cents"] = 65.
    return frame


def admitted_fixture():
    config = config_fixture()
    quotes = audit.normalize_book_rows(raw_fixture())
    decision = quotes.iloc[0].ts_utc + pd.Timedelta(seconds=120)
    record, reason = audit.prefix_book_features(quotes[quotes.ts_utc <= decision], run_name="new",
        slug=quotes.iloc[0].slug, decision_ts=decision, config=config)
    assert reason == "admitted"
    return config, quotes, record


class OfflineBidResidualAuditTests(unittest.TestCase):
    def test_prefix_does_not_read_terminal_depth_or_future_columns(self):
        config, quotes, record = admitted_fixture()
        raw = raw_fixture()
        raw["target_price"] = "untrusted"
        raw["final_price"] = "untrusted"
        raw["bid_depth_up_5"] = "untrusted"
        raw["mid_up_cents"] = 99.
        raw["spread_up_cents"] = -999.
        raw.loc[raw.ts_iso > record["decision_ts"], "buy_up_cents"] = 1.
        changed = audit.normalize_book_rows(raw)
        result, reason = audit.prefix_book_features(changed[changed.ts_utc <= record["decision_ts"]],
            run_name="new", slug=record["slug"], decision_ts=record["decision_ts"], config=config)
        self.assertEqual(reason, "admitted")
        self.assertEqual(record, result)
        self.assertFalse(set(["target_price", "final_price", "bid_depth_up_5", "mid_up_cents"]) & set(changed.columns))
        self.assertAlmostEqual(record["spread_up_median_first2m"], .02)

    def test_two_sided_favorite_with_overround_and_undefined_ratio(self):
        raw = raw_fixture()
        raw["buy_up_cents"], raw["sell_up_cents"] = 54., 50.
        raw["buy_down_cents"], raw["sell_down_cents"] = 59., 55.
        quotes = audit.normalize_book_rows(raw)
        cutoff = quotes.iloc[0].ts_utc + pd.Timedelta(seconds=120)
        prefix = quotes[quotes.ts_utc <= cutoff]
        record, _ = audit.prefix_book_features(prefix, run_name="new", slug=prefix.iloc[0].slug,
            decision_ts=cutoff, config=config_fixture())
        self.assertEqual(record["side"], "down")
        self.assertAlmostEqual(record["mid_up_prob_2m"], .52)
        self.assertAlmostEqual(record["overround_median_first2m"], .09)
        broken = prefix.copy(); broken.loc[broken.index[-1], ["buy_up_size", "buy_down_size"]] = 0.
        record, reason = audit.prefix_book_features(broken, run_name="new", slug=prefix.iloc[0].slug,
            decision_ts=cutoff, config=config_fixture())
        self.assertIsNone(record)
        self.assertEqual(reason, "nonfinite_registered_book_feature")

    def test_prefix_future_input_and_stale_or_late_opening_are_rejected(self):
        config, quotes, record = admitted_fixture()
        with self.assertRaisesRegex(ValueError, "decision_prefix"):
            audit.prefix_book_features(quotes, run_name="new", slug=record["slug"], decision_ts=record["decision_ts"], config=config)
        late = quotes.copy(); late["ts_utc"] += pd.Timedelta(seconds=4)
        cutoff = record["decision_ts"] + pd.Timedelta(seconds=4)
        value, reason = audit.prefix_book_features(late[late.ts_utc <= cutoff], run_name="new",
            slug=record["slug"], decision_ts=cutoff, config=config)
        self.assertIsNone(value); self.assertEqual(reason, "opening_clock_not_covered")
        prefix = quotes[quotes.ts_utc < record["decision_ts"]].copy()
        extra = prefix.iloc[[1]].copy(); extra["ts_utc"] += pd.Timedelta(seconds=30)
        prefix = pd.concat([prefix, extra], ignore_index=True)
        value, reason = audit.prefix_book_features(prefix, run_name="new", slug=record["slug"],
            decision_ts=record["decision_ts"], config=config)
        self.assertIsNone(value); self.assertEqual(reason, "invalid_or_stale_prefix_snapshot")

    def test_incomplete_or_crossed_intermediate_quote_cannot_hide_in_median(self):
        config, quotes, record = admitted_fixture()
        prefix = quotes[quotes.ts_utc <= record["decision_ts"]].copy()
        for value in (np.nan, 50.):
            with self.subTest(buy_up_cents=value):
                extra = prefix.iloc[[0]].copy()
                extra["ts_utc"] += pd.Timedelta(seconds=30)
                extra["buy_up_cents"] = value
                changed = pd.concat([prefix, extra], ignore_index=True)
                result, reason = audit.prefix_book_features(changed, run_name="new", slug=record["slug"],
                    decision_ts=record["decision_ts"], config=config)
                self.assertIsNone(result)
                self.assertEqual(reason, "incomplete_or_invalid_prefix_book")

    def test_label_uses_actual_entry_clock_and_never_exit_depth(self):
        config, quotes, record = admitted_fixture()
        quotes.loc[quotes.ts_utc == pd.Timestamp("2026-01-02T00:02:52Z"), "sell_up_size"] = 0.
        target = audit.quote_target(record, quotes, config)
        self.assertEqual(target["target_entry_quote_ts"], pd.Timestamp("2026-01-02T00:02:14Z"))
        self.assertEqual(target["target_exit_arrival_ts"], pd.Timestamp("2026-01-02T00:02:52Z"))
        self.assertEqual(target["label_available_ts"], target["target_exit_arrival_ts"])
        self.assertAlmostEqual(target["future_bid_price"], .65)
        self.assertAlmostEqual(target["bid_residual_target"], .07)

    def test_future_conflict_or_missing_exit_does_not_remove_test_cohort(self):
        config = config_fixture(["new"])
        raw = raw_fixture()
        future_conflict = raw.iloc[[5]].copy(); future_conflict["sell_up_cents"] = 66.
        for changed in (raw.iloc[:4], pd.concat([raw, future_conflict], ignore_index=True)):
            with self.subTest(rows=len(changed)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / "new").mkdir()
                changed.to_csv(root / "new" / "part_btc-updown-5m_quotes.csv", index=False)
                frame, books, coverage = audit.prepare_archive(root, config)
                self.assertEqual(len(frame), 1)
                self.assertTrue(pd.isna(frame.iloc[0].future_bid_price))
                self.assertTrue(pd.isna(frame.iloc[0].label_available_ts))
                self.assertEqual(coverage["prefix_admission"], {"admitted": 1})
                if len(changed) > len(raw):
                    self.assertIn("conflict", frame.iloc[0].target_status)
                    self.assertTrue(books[frame.iloc[0].market_id].quote_conflict.any())

    def test_prefix_conflict_rejects_but_global_owner_never_changes_to_later_run(self):
        config = config_fixture(["old", "new"])
        raw = raw_fixture(); duplicate = raw.iloc[[2]].copy(); duplicate["buy_up_cents"] = 61.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in config["source_runs"]:
                (root / name).mkdir()
                part = pd.concat([raw, duplicate], ignore_index=True) if name == "old" else raw
                part.to_csv(root / name / "part_btc-updown-5m_quotes.csv", index=False)
            frame, _, coverage = audit.prepare_archive(root, config)
            self.assertTrue(frame.empty)
            self.assertEqual(coverage["prefix_admission"], {"conflicting_prefix_quotes": 1})
            self.assertEqual(coverage["duplicate_market_sources_ignored"], 1)

    def test_training_embargo_strictly_excludes_equal_future_and_unknown_labels(self):
        cutoff = pd.Timestamp("2026-01-02T00:00:00Z")
        frame = pd.DataFrame({"run_name": ["old"] * 4, "market_id": ["old::" + str(i) for i in range(4)],
            "slug": [str(i) for i in range(4)], "bid_residual_target": [.1, .2, .3, np.nan],
            "label_available_ts": pd.to_datetime([cutoff - pd.Timedelta(seconds=1), cutoff,
                cutoff + pd.Timedelta(seconds=1), None], utc=True)})
        current = pd.DataFrame({"decision_ts": [cutoff], "slug": ["new"]})
        train = audit.causal_training_rows(frame, ["old"], current)
        self.assertEqual(train.market_id.tolist(), ["old::0"])

    def test_scaler_is_fit_only_on_training_and_decision_forecast_is_clipped(self):
        config = config_fixture()
        train = pd.DataFrame({name: np.linspace(0, 1, 100) for name in audit.FEATURES})
        train["bid_residual_target"] = np.linspace(-.2, .2, 100)
        current = pd.DataFrame({name: [-100., 100.] for name in audit.FEATURES})
        current["decision_bid_price"] = .5
        scored, model = audit.fit_predict_once(train, current, config)
        np.testing.assert_allclose(model["scaler_mean"], train[audit.FEATURES].mean().to_numpy())
        self.assertEqual(scored.predicted_exit_bid_price.tolist(), [0., 1.])
        self.assertTrue((scored.predicted_exit_bid_unclipped.abs() > 1).all())

    def test_mse_uses_clipped_decision_bid_and_retains_unknown_denominator(self):
        scores = pd.DataFrame({"run_name": ["new", "new"], "future_bid_price": [.9, np.nan],
            "decision_bid_price": [.5, .5], "predicted_exit_bid_price": [1., 1.],
            "predicted_exit_bid_unclipped": [3., 3.]})
        result = audit.prediction_metrics(scores)
        self.assertAlmostEqual(result["aggregate_known_targets"]["model_mse"], .01)
        self.assertEqual(result["unknown_targets_retained"], 1)
        self.assertEqual(result["aggregate_known_targets"]["known_targets"], 1)

    def test_evaluate_predicts_unknown_test_rows_and_reports_real_fit_counts(self):
        config = config_fixture()
        train = pd.DataFrame({name: np.linspace(.1, .9, 100) for name in audit.FEATURES})
        train = train.assign(run_name="old", slug=["train-" + str(i) for i in range(100)],
            market_id=["old::" + str(i) for i in range(100)], decision_ts=pd.Timestamp("2026-01-01T00:00:00Z"),
            label_available_ts=pd.Timestamp("2026-01-01T00:03:00Z"), bid_residual_target=.3,
            future_bid_price=.8, decision_bid_price=.5, decision_ask_price=.51)
        current = pd.DataFrame({name: [.4, .6] for name in audit.FEATURES})
        current = current.assign(run_name="new", slug=["current-1", "current-2"], market_id=["new::1", "new::2"],
            decision_ts=pd.Timestamp("2026-01-02T00:00:00Z"), decision_bid_price=.5, decision_ask_price=.51,
            label_available_ts=pd.to_datetime(["2026-01-02T00:03:00Z", None], utc=True),
            future_bid_price=[.52, np.nan], bid_residual_target=[.02, np.nan])
        features = pd.concat([train, current], ignore_index=True)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(audit.integrity, "verified_commits", return_value=("a" * 40, "b" * 40)), \
             patch.object(audit, "prepare_archive", return_value=(features, {}, {})), \
             patch.object(audit.execution, "replay_timed_quotes", return_value={"fills": 0, "ending_cash": 10., "chain": [], "attempts": []}) as replay:
            result = audit.evaluate(Path(directory), config, Path(directory) / "out")
            self.assertTrue(result["strategy_evaluated"])
            self.assertEqual(result["model_scored_rows"], 2)
            self.assertEqual(result["actual_model_fit_calls"], 1)
            self.assertEqual(result["actual_model_predict_calls"], 1)
            self.assertEqual(result["prediction_metrics"]["unknown_targets_retained"], 1)
            self.assertEqual(replay.call_count, 4)
            self.assertEqual([len(call.args[0]) for call in replay.call_args_list], [2, 2, 0, 0])
            members = pd.read_csv(Path(directory) / "out" / "training-members.csv")
            self.assertEqual(len(members), 100)
            self.assertIn("bid_residual_target", members.columns)
            for path in (Path(directory) / "out").iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_enough_labels_from_one_run_do_not_replace_required_labeled_runs(self):
        config = config_fixture(["old", "gap1", "gap2", "gap3", "new"])
        config["minimum_training_runs"] = 4
        train = pd.DataFrame({name: np.linspace(.1, .9, 100) for name in audit.FEATURES})
        train = train.assign(run_name="old", slug=["train-" + str(i) for i in range(100)],
            market_id=["old::" + str(i) for i in range(100)], decision_ts=pd.Timestamp("2026-01-01T00:00:00Z"),
            label_available_ts=pd.Timestamp("2026-01-01T00:03:00Z"), bid_residual_target=.1,
            future_bid_price=.6, decision_bid_price=.5, decision_ask_price=.51)
        current = train.iloc[:1].copy()
        current["run_name"] = "new"; current["slug"] = "different"; current["market_id"] = "new::different"
        current["decision_ts"] = pd.Timestamp("2026-01-02T00:00:00Z")
        features = pd.concat([train, current], ignore_index=True)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(audit.integrity, "verified_commits", return_value=("a" * 40, "b" * 40)), \
             patch.object(audit, "prepare_archive", return_value=(features, {}, {})), \
             patch.object(audit, "fit_predict_once", side_effect=AssertionError("must not fit")):
            result = audit.evaluate(Path(directory), config, Path(directory) / "out")
            self.assertFalse(result["strategy_evaluated"])
            self.assertEqual(result["actual_model_fit_calls"], 0)
            self.assertEqual(result["fit_records"][0]["training_known_runs"], 1)
            self.assertEqual(result["fit_records"][0]["status"], "insufficient_prior_labeled_runs")

    def test_decision_cost_gate_does_not_consult_future_targets(self):
        opportunities = pd.DataFrame({"decision_ask_price": [.7, .7], "predicted_exit_bid_price": [.9, .7],
            "future_bid_price": [np.nan, 1.]})
        selected, decisions = audit.decision_cost_candidates(opportunities, config_fixture())
        self.assertEqual(selected.index.tolist(), [0])
        self.assertTrue(pd.isna(selected.iloc[0].future_bid_price))
        self.assertEqual(decisions.decision_signal_admitted.tolist(), [True, False])


if __name__ == "__main__":
    unittest.main()
