import copy
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal

from analysis.v3_data_contract import validate_record
from analysis.walk_forward_execution import (
    EvaluationBlocked,
    WalkForwardConfig,
    brier_score,
    chronological_folds,
    evaluate,
    expected_calibration_error,
    feature_key,
    fit_model,
    main as execution_main,
    run_files,
    simulate_order,
)


def observation(market, when, *, ask="0.20", ask_size="10", bid="0.19", bid_size="10", observation_id=None):
    return {
        "schema_version": 3,
        "observation_id": observation_id or f"obs-{market}-{when}",
        "source_event_time_ms": when,
        "received_time_ms": when + 100,
        "market_id": market,
        "condition_id": f"condition-{market}",
        "token_ids": {"up": f"up-{market}", "down": f"down-{market}"},
        "observation_type": "book_snapshot",
        "fees": {"rate": 0.07, "exponent": 1, "asset": "USDC"},
        "rules": {"tick_size": 0.01, "min_order_size": 5},
        "provenance": {
            "source": "fixture", "capture_id": "walk-forward-test",
            "source_sha256": "a" * 64, "retrieved_at_ms": when + 100,
        },
        "books": {
            "up": {"bids": [{"level": 0, "price": float(bid), "size": float(bid_size)}],
                   "asks": [{"level": 0, "price": float(ask), "size": float(ask_size)}]},
            "down": {"bids": [{"level": 0, "price": float(bid), "size": float(bid_size)}],
                     "asks": [{"level": 0, "price": float(ask), "size": float(ask_size)}]},
        },
    }


def label(market, when, outcome="up", available=None):
    return {
        "label_version": 1,
        "market_id": market,
        "condition_id": f"condition-{market}",
        "outcome": outcome,
        "resolved_time_ms": when,
        "label_available_time_ms": available if available is not None else when + 100,
        "source": "fixture-resolution",
        "source_sha256": "b" * 64,
    }


class WalkForwardExecutionTests(unittest.TestCase):
    def test_cli_requires_preregistration_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            self.assertEqual(execution_main([
                "--observations", str(Path(__file__).parent / "fixtures" / "v3_observation_valid.jsonl"),
                "--labels", str(Path(__file__).parent / "fixtures" / "walk_forward_labels_valid.jsonl"),
                "--output", str(output),
            ]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("missing_preregistration_manifest", report["blocked_reasons"])
        self.assertEqual(report["metrics"]["brier"], None)

    def test_cli_synthetic_preregistration_gate_has_no_oos_metrics(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            self.assertEqual(execution_main([
                "--observations", str(root / "tests" / "fixtures" / "v3_observation_valid.jsonl"),
                "--labels", str(root / "tests" / "fixtures" / "walk_forward_labels_valid.jsonl"),
                "--manifest", str(root / "research" / "strategy" / "experiments" / "EXP-0002-btc5m-preregistered-reference.json"),
                "--code-commit", "c3ff9549ad0b62ec19fa21c7d917781cd5e4273a",
                "--evaluation-dates", "2026-10-03", "--synthetic", "--output", str(output),
            ]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("synthetic_input_not_evidence", report["blocked_reasons"])
        self.assertIsNone(report["metrics"]["brier"])
        self.assertIsNone(report["preregistration"]["metrics"]["oos"])

    def test_mid_price_is_rejected(self):
        decision = observation("m1", 1_000)
        with self.assertRaisesRegex(EvaluationBlocked, "MID_PRICE_NOT_EXECUTABLE"):
            simulate_order(decision, [decision], token="up", side="buy", quantity="5",
                           latency_ms=0, ttl_ms=1_000, price_source="mid", limit_price="0.195")
        with self.assertRaisesRegex(EvaluationBlocked, "MID_PRICE_NOT_EXECUTABLE"):
            tick_aligned = observation("m1", 1_000, ask="0.21", bid="0.19")
            simulate_order(tick_aligned, [observation("m1", 1_200, ask="0.21", bid="0.19")], token="up", side="buy", quantity="5",
                           latency_ms=0, ttl_ms=1_000, limit_price="0.20")

    def test_partial_fill_and_expiry_are_explicit(self):
        decision = observation("m1", 1_000, ask_size="2")
        later = observation("m1", 2_100, ask_size="2")
        result = simulate_order(decision, [later], token="up", side="buy", quantity="5",
                                latency_ms=1_000, ttl_ms=5_000)
        self.assertEqual(result["status"], "partial_canceled")
        self.assertEqual(Decimal(result["filled_shares"]), Decimal("2"))
        expired = simulate_order(decision, [], token="up", side="buy", quantity="5",
                                 latency_ms=1_000, ttl_ms=5_000)
        self.assertEqual(expired["status"], "unknown_execution")
        self.assertIsNone(expired["filled_shares"])

    def test_future_rows_and_labels_cannot_enter_fit(self):
        first = observation("m1", 1_000, ask="0.20", observation_id="old-row")
        future = observation("m1", 9_000, ask="0.90", observation_id="future-row")
        other = observation("m2", 2_000, ask="0.20", observation_id="other-row")
        labels = {"m1": label("m1", 1_500, available=2_500), "m2": label("m2", 2_500, available=2_600)}
        model = fit_model([first, future, other], labels, train_market_ids={"m1", "m2"},
                          cutoff_ms=4_000, min_training_events=1)
        self.assertEqual(model["training_observation_ids"], ["old-row", "other-row"])
        self.assertNotIn("future-row", model["training_observation_ids"])
        contaminated = copy.deepcopy(first)
        contaminated["outcome"] = "up"
        ok, reasons = validate_record(contaminated)
        self.assertFalse(ok)
        self.assertTrue(any("forbidden_future_field" in reason for reason in reasons))

    def test_labels_after_cutoff_are_not_training_data(self):
        rows = [observation("m1", 1_000)]
        labels = {"m1": label("m1", 1_500, available=9_000)}
        with self.assertRaisesRegex(EvaluationBlocked, "insufficient_pre_cutoff_training_events"):
            fit_model(rows, labels, train_market_ids={"m1"}, cutoff_ms=4_000, min_training_events=1)

    def test_market_clusters_are_disjoint_and_purged(self):
        rows = [observation(f"m{i}", i * 1_000) for i in range(1, 8)]
        config = WalkForwardConfig(train_duration_ms=2_000, test_duration_ms=2_000,
                                   purge_ms=1_000, embargo_ms=500, min_training_events=1)
        folds = chronological_folds(rows, config)
        self.assertTrue(folds)
        for fold in folds:
            self.assertTrue(set(fold["train_market_ids"]).isdisjoint(fold["test_market_ids"]))
            self.assertEqual(fold["test_start_ms"], fold["train_end_ms"] + 1_000 + 500)

    def test_brier_and_ece_are_reported(self):
        rows = [{"predicted_prob_up": 0.8, "actual_up": 1},
                {"predicted_prob_up": 0.2, "actual_up": 0}]
        self.assertAlmostEqual(brier_score(rows), 0.04)
        self.assertAlmostEqual(expected_calibration_error(rows), 0.2)

    def test_end_to_end_report_has_cost_adjusted_metrics_and_stays_blocked(self):
        rows = [observation(f"m{i}", i * 1_000, ask="0.20", ask_size="10") for i in range(1, 13)]
        rows.append(observation("test", 20_000, ask="0.20", ask_size="10"))
        rows.append(observation("test", 21_100, ask="0.20", ask_size="10"))
        labels = {f"m{i}": label(f"m{i}", i * 1_000 + 200, available=i * 1_000 + 300) for i in range(1, 13)}
        labels["test"] = label("test", 22_000, available=22_100)
        config = WalkForwardConfig(train_duration_ms=12_000, test_duration_ms=10_000,
                                   purge_ms=100, embargo_ms=100, latency_ms=1_000,
                                   order_ttl_ms=5_000, order_size=Decimal("5"),
                                   min_training_events=10)
        report = evaluate(rows, labels, config)
        self.assertTrue(report["canary_blocked"])
        self.assertGreaterEqual(report["metrics"]["predictions"], 1)
        self.assertIsNotNone(report["metrics"]["brier"])
        self.assertIsNotNone(report["metrics"]["ece"])
        self.assertIsNotNone(report["metrics"]["net_pnl_usdc"])
        self.assertIn("private_execution_receipts_required_for_canary", report["blocked_reasons"])

    def test_missing_inputs_report_machine_readable_minimum_package(self):
        config = WalkForwardConfig(min_training_events=10)
        report = run_files(
            Path("/private/observations-v3.jsonl"),
            Path("/private/resolution-labels.jsonl"),
            config,
        )
        self.assertTrue(report["canary_blocked"])
        self.assertEqual(
            report["blocked_reasons"], ["missing_observation_file", "missing_label_file"]
        )
        requirements = report["input_requirements"]
        self.assertFalse(requirements["observations"]["present"])
        self.assertFalse(requirements["resolution_labels"]["present"])
        self.assertIn("source_event_time_ms", requirements["observations"]["minimum_record"]["required_fields"])
        self.assertIn("label_available_time_ms", requirements["resolution_labels"]["minimum_record"]["required_fields"])
        self.assertEqual(requirements["observations"]["provided_name"], "observations-v3.jsonl")
        self.assertEqual(requirements["resolution_labels"]["provided_name"], "resolution-labels.jsonl")
        self.assertEqual(requirements["canary_gate_context"]["minimum_independent_windows"], 300)
        self.assertFalse(requirements["canary_gate_context"]["enforced_by_this_public_replay"])
        self.assertIn("private order/fill", requirements["canary_boundary"])


if __name__ == "__main__":
    unittest.main()
