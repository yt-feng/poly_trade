"""Counterexamples enter through real files and the registered execution CLI."""
import copy
from dataclasses import asdict
from decimal import Decimal
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from analysis.preregistered_strategy import file_sha256, manifest_digest, parameter_grid_digest
from analysis.walk_forward_execution import WalkForwardConfig, _observation_features, main, run_files, simulate_order
from test_walk_forward_execution import observation, label

ROOT = Path(__file__).resolve().parents[1]
CONFIG = WalkForwardConfig(train_duration_ms=12_000, test_duration_ms=10_000,
                          purge_ms=100, embargo_ms=100, latency_ms=1_000,
                          min_training_events=10)


def sample():
    rows = [observation(f"m{i}", i * 1000) for i in range(1, 13)]
    labels = [label(f"m{i}", i * 1000 + 200, available=i * 1000 + 300) for i in range(1, 13)]
    rows.extend([observation("test", 20_000), observation("test", 21_100)])
    labels.append(label("test", 22_000, available=22_100))
    return rows, labels


def run_sample(rows, labels, cli=False):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        obs, lab = root / "observations.jsonl", root / "labels.jsonl"
        obs.write_text("".join(json.dumps(row) + "\n" for row in rows))
        lab.write_text("".join(json.dumps(row) + "\n" for row in labels))
        if not cli:
            return run_files(obs, lab, CONFIG)
        # Freeze exact synthetic files and config, then exercise the real CLI.
        # Synthetic test data is never a checked-in performance artifact.
        config = {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(CONFIG).items()}
        manifest = json.loads((ROOT / "research/strategy/experiments/EXP-0002-btc5m-preregistered-reference.json").read_text())
        manifest["data"].update(observations_sha256=file_sha256(obs), labels_sha256=file_sha256(lab))
        for key, value in config.items():
            if key != "edge_buffer":
                manifest["parameter_grid"][key] = [value]
        manifest["parameter_grid_sha256"] = parameter_grid_digest(manifest["parameter_grid"])
        manifest["manifest_sha256"] = manifest_digest(manifest)
        (root / "config.json").write_text(json.dumps(config))
        (root / "manifest.json").write_text(json.dumps(manifest))
        output = root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--observations", str(obs), "--labels", str(lab),
                  "--manifest", str(root / "manifest.json"), "--config", str(root / "config.json"),
                  "--code-commit", manifest["code"]["commit"], "--evaluation-dates", "2026-10-03",
                  "--output", str(output)])
        report = json.loads(output.read_text())
        if report.get("preregistration", {}).get("blockers"):
            raise AssertionError(report["preregistration"]["blockers"])
        return report


class ExecutionPathRegressions(unittest.TestCase):
    def assert_no_metrics(self, report, reason):
        self.assertIn(reason, report["blocked_reasons"])
        self.assertIsNone(report["metrics"]["brier"])
        self.assertIsNone(report["metrics"]["net_pnl_usdc"])

    def test_cli_executes_intake_and_actual_split_audit(self):
        report = run_sample(*sample(), cli=True)
        self.assertEqual(report["split_audit"]["blocked_reasons"], [])
        self.assertEqual(report["metrics"]["filled_attempts"], 1)
        self.assertIsNotNone(report["metrics"]["net_pnl_usdc"])
        self.assertTrue(report["canary_blocked"])
        self.assertIn("fewer_than_300_independent_windows", report["intake"]["blocked_reasons"])

    def test_cli_invalid_or_duplicate_rows_never_shrink_profit_sample(self):
        for mutation in ("missing_rules", "duplicate", "label_duplicate", "label_bad"):
            with self.subTest(mutation=mutation):
                rows, labels = sample()
                if mutation == "missing_rules":
                    rows[-1].pop("rules")
                elif mutation == "duplicate":
                    rows.append(copy.deepcopy(rows[-1]))
                elif mutation == "label_duplicate":
                    labels.append(copy.deepcopy(labels[-1]))
                else:
                    labels[-1]["source_sha256"] = "invalid"
                report = run_sample(rows, labels, cli=True)
                self.assert_no_metrics(report, "quarantined_label_records" if mutation.startswith("label") else "quarantined_observation_records")
                self.assertEqual(report["input"]["observation_records"], len(rows))

    def test_cli_identity_and_label_causality_fail_before_model(self):
        cases = {
            "token_change": "inconsistent_market_condition_or_token_identity",
            "wrong_condition": "label_condition_mismatch",
            "known_label": "label_available_before_or_at_observation",
            "post_resolution": "observation_at_or_after_resolution",
            "condition_alias": "condition_aliased_across_markets",
            "time_regression": "observation_time_not_monotonic_by_market",
        }
        for case, reason in cases.items():
            with self.subTest(case=case):
                rows, labels = sample()
                if case == "token_change": rows[-1]["token_ids"]["up"] = "different-token"
                if case == "wrong_condition": labels[-1]["condition_id"] = "wrong"
                if case == "known_label": labels[-1].update(resolved_time_ms=19_000, label_available_time_ms=19_100)
                if case == "post_resolution": labels[-1]["resolved_time_ms"] = 20_050
                if case == "condition_alias":
                    rows[0]["condition_id"] = rows[1]["condition_id"]
                    labels[0]["condition_id"] = labels[1]["condition_id"]
                if case == "time_regression": rows[-1], rows[-2] = rows[-2], rows[-1]
                self.assert_no_metrics(run_sample(rows, labels, cli=True), reason)

    def test_cli_cluster_boundary_and_training_label_cutoff_block(self):
        rows, labels = sample()
        rows.insert(12, observation("m12", 13_100))
        labels[11].update(resolved_time_ms=13_300, label_available_time_ms=13_400)
        self.assert_no_metrics(run_sample(rows, labels, cli=True), "cluster_crosses_split_boundary")
        rows, labels = sample()
        labels[11]["label_available_time_ms"] = 13_400
        self.assert_no_metrics(run_sample(rows, labels, cli=True), "train_label_after_availability_cutoff")

    def test_positive_depth_touch_ignores_zero_and_sorts_prices(self):
        row = observation("test", 20_000)
        row["books"]["up"]["asks"] = [
            {"level": 0, "price": 0.01, "size": 0},
            {"level": 1, "price": 0.30, "size": 5},
            {"level": 2, "price": 0.20, "size": 5}]
        row["books"]["up"]["bids"] = [
            {"level": 0, "price": 0.99, "size": 0},
            {"level": 1, "price": 0.10, "size": 5},
            {"level": 2, "price": 0.19, "size": 5}]
        features = _observation_features(row)
        self.assertEqual(features["up_ask"], Decimal("0.2"))
        self.assertEqual(features["up_bid"], Decimal("0.19"))
        self.assertEqual(features["up_ask_size"], Decimal("5"))

    def test_cli_zero_depth_does_not_drop_bad_market(self):
        rows, labels = sample()
        rows[-1]["books"]["up"]["asks"][0]["size"] = 0
        self.assert_no_metrics(run_sample(rows, labels, cli=True), "feature_extraction_failed_without_sample_shrink")

    def test_cli_missing_post_order_feed_is_unknown_and_no_pnl(self):
        rows, labels = sample()
        report = run_sample(rows[:-1], labels, cli=True)
        self.assertEqual(report["metrics"]["unknown_orders"], 1)
        self.assertEqual(report["metrics"]["expired_orders"], 0)
        self.assertIsNone(report["metrics"]["net_pnl_usdc"])
        self.assertIn("unknown_execution_outcomes", report["blocked_reasons"])

    def test_one_unknown_attempt_invalidates_aggregate_even_with_a_fill(self):
        rows, labels = sample()
        rows.append(observation("unknown", 22_100))
        labels.append(label("unknown", 23_000, available=23_100))
        report = run_sample(rows, labels, cli=True)
        self.assertEqual(report["metrics"]["filled_attempts"], 1)
        self.assertEqual(report["metrics"]["unknown_orders"], 1)
        self.assertEqual(report["metrics"]["attempts"], 2)
        self.assertIsNone(report["metrics"]["net_pnl_usdc"])

    def test_receipt_time_regression_cannot_hide_behind_increasing_source_time(self):
        rows, labels = sample()
        rows[-2]["received_time_ms"] = 21_000
        rows[-1].update(source_event_time_ms=20_100, received_time_ms=20_900)
        self.assert_no_metrics(run_sample(rows, labels, cli=True), "observation_time_not_monotonic_by_market")

    def test_cli_delayed_pre_order_and_stale_events_cannot_fill(self):
        for event, received in ((20_500, 21_200), (21_100, 23_000), (22_800, 22_900)):
            with self.subTest(event=event, received=received):
                rows, labels = sample()
                rows[-1].update(source_event_time_ms=event, received_time_ms=received)
                labels[-1].update(resolved_time_ms=24_000, label_available_time_ms=24_100)
                report = run_sample(rows, labels, cli=True)
                self.assertEqual(report["metrics"]["unknown_orders"], 1)
                self.assertEqual(report["metrics"]["filled_attempts"], 0)
                self.assertIsNone(report["metrics"]["net_pnl_usdc"])

    def test_later_crossing_fills_but_missing_ttl_is_unknown(self):
        decision = observation("m", 1000)
        rows = [observation("m", 2200, ask="0.3"), observation("m", 3100, ask="0.2")]
        args = dict(token="up", side="buy", quantity=5, latency_ms=1000, ttl_ms=5000, limit_price="0.2")
        result = simulate_order(decision, rows, **args)
        self.assertEqual(result["status"], "filled")
        self.assertGreaterEqual(result["fill_source_event_time_ms"], result["active_at_ms"])
        self.assertEqual(simulate_order(decision, rows[:1], **args)["status"], "unknown_execution")

    def test_expired_requires_fresh_causal_books_through_ttl(self):
        decision = observation("m", 1000)
        rows = [observation("m", when, ask="0.3") for when in (2100, 3100, 4100, 5100, 6100)]
        rows[-1]["received_time_ms"] = 6100
        result = simulate_order(decision, rows, token="up", side="buy", quantity=5,
                                latency_ms=1000, ttl_ms=5000, limit_price="0.2")
        self.assertEqual(result["status"], "expired_unfilled")
        self.assertIn("simulation_only", result["expiry_evidence"])


if __name__ == "__main__":
    unittest.main()
