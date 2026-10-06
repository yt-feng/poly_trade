import copy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from analysis.quote_factor_diagnostic import (
    FEATURES, TARGETS, coverage, diagnose, load_frozen_quotes, run,
    scheduled_samples, validate_protocol,
)
from analysis.archive_baseline_audit import timestamp, PRICE_FIELDS, SIZE_FIELDS


def protocol():
    return {
        "train_dates": ["2026-09-23"], "validation_dates": ["2026-09-24"],
        "window_seconds": 300, "target_token": "up", "decision_offset_seconds": 120,
        "lookback_seconds": 15, "horizon_seconds": 30, "max_quote_age_seconds": 3,
        "future_tolerance_seconds": 3, "stability_block_hours": 6,
        "min_train_labels": 2, "min_validation_labels": 2, "ridge_penalty": 1.0,
        "quantile_cuts": [0.2, 0.8], "features": list(FEATURES), "targets": list(TARGETS),
        "models": {"zero": [], "train_mean": [], "momentum": ["momentum"],
                   "momentum_spread": ["momentum", "spread"], "all": list(FEATURES)},
        "files": [],
    }


def quote(start, offset, level, *, invalid=None, size=10):
    return {"ts": start + offset, "values": (level + 1, level, 50, 49, 10, size, 10, 10),
            "invalid_reason": invalid}


def fixture():
    p = protocol()
    grouped = {}
    for date in p["train_dates"] + p["validation_dates"]:
        for i in range(3):
            start = int(timestamp(date + "T00:00:00Z")) + i * 300
            grouped[f"btc-updown-5m-{start}"] = [quote(start, 104, 40), quote(start, 119, 41 + i, size=10 + i),
                                                  quote(start, 150, 42 + i)]
    return p, grouped


class QuoteFactorDiagnosticTests(unittest.TestCase):
    def test_full_market_calendar_remains_in_denominator(self):
        p, grouped = fixture()
        samples = scheduled_samples(grouped, set(), p)
        report = diagnose(samples, p)
        self.assertEqual(len(samples), 576)
        for split in ("train", "validation"):
            self.assertEqual(report["coverage"][split]["scheduled_windows"], 288)
            self.assertEqual(report["coverage"][split]["labeled_windows"], 3)
            self.assertEqual(report["coverage"][split]["status_counts"]["absent_window"], 285)
        self.assertFalse({x["market"] for x in samples if x["split"] == "train"} &
                         {x["market"] for x in samples if x["split"] == "validation"})

    def test_future_mutation_cannot_change_features_or_train_fit(self):
        p, grouped = fixture()
        before_samples = scheduled_samples(grouped, set(), p)
        before = diagnose(before_samples, p)
        for market in list(grouped)[3:]:
            start = int(market.rsplit("-", 1)[1])
            grouped[market][-1] = quote(start, 150, 10)
        after_samples = scheduled_samples(grouped, set(), p)
        self.assertEqual([x["features"] for x in before_samples], [x["features"] for x in after_samples])
        after = diagnose(after_samples, p)
        for name in p["models"]:
            self.assertEqual(before["targets"][TARGETS[0]]["models"][name]["fit"],
                             after["targets"][TARGETS[0]]["models"][name]["fit"])

    def test_future_unknown_preserves_denominators_and_loss_bounds(self):
        p, grouped = fixture()
        grouped[list(grouped)[-1]].pop()
        samples = scheduled_samples(grouped, set(), p)
        report = diagnose(samples, p)
        cov = report["coverage"]["validation"]
        self.assertEqual(cov["labeled_windows"], 2)
        self.assertEqual(cov["unknown_future_windows"], 1)
        self.assertEqual(cov["feature_eligible_windows"], 3)
        for result in report["targets"][TARGETS[0]]["models"].values():
            score = result["validation"]
            self.assertEqual((score["n_labeled"], score["n_unknown"], score["n_scheduled"]), (2, 1, 288))
            self.assertLess(score["feature_eligible_mse_lower_bound"], score["feature_eligible_mse_upper_bound"])

    def test_first_bad_future_quote_not_skipped_for_later_pretty_quote(self):
        p, grouped = fixture()
        market = list(grouped)[-1]
        start = int(market.rsplit("-", 1)[1])
        grouped[market][-1]["invalid_reason"] = "nonpositive_touch_size"
        grouped[market].append(quote(start, 151, 90))
        row = next(x for x in scheduled_samples(grouped, set(), p) if x["market"] == market)
        self.assertEqual(row["status"], "unknown_future_invalid")
        self.assertIsNone(row["targets"])

    def test_decision_at_cutoff_not_a_feature_and_label_stays_inside_market(self):
        p, grouped = fixture()
        market = list(grouped)[0]
        start = int(market.rsplit("-", 1)[1])
        grouped[market].insert(2, quote(start, 120, 90))
        row = next(x for x in scheduled_samples(grouped, set(), p) if x["market"] == market)
        self.assertEqual(row["feature_times"][-1], start + 119)
        self.assertEqual(row["features"]["momentum"], 1)
        self.assertLess(row["target_time"], start + 300)

    def test_conflict_is_a_whole_window_exclusion(self):
        p, grouped = fixture()
        market = next(iter(grouped))
        samples = scheduled_samples(grouped, {market}, p)
        row = next(x for x in samples if x["market"] == market)
        self.assertEqual(row["status"], "excluded_conflicting_window")
        self.assertIsNone(row["features"])

    def test_overlap_or_insufficient_training_never_produces_effects(self):
        p, grouped = fixture()
        bad = copy.deepcopy(p)
        bad["validation_dates"] = bad["train_dates"]
        with self.assertRaisesRegex(ValueError, "overlapping"):
            validate_protocol(bad)
        p["min_train_labels"] = 4
        report = diagnose(scheduled_samples(grouped, set(), p), p)
        self.assertEqual(report["targets"], {})
        self.assertIn("insufficient_training_labels", report["failures"])

    def test_ablations_and_time_blocks_use_same_labeled_cohort(self):
        p, grouped = fixture()
        result = diagnose(scheduled_samples(grouped, set(), p), p)
        for target in TARGETS:
            report = result["targets"][target]
            self.assertEqual(set(report["models"]), set(p["models"]))
            self.assertEqual({m["validation"]["n_labeled"] for m in report["models"].values()}, {3})
            self.assertEqual(sum(x["coverage"]["scheduled_windows"] for x in report["validation_time_blocks"].values()), 288)

    def test_real_file_hash_check_ignores_settlement_fields_and_oi(self):
        p, grouped = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "quotes.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["ts_iso", "slug", "final_price"] + list(PRICE_FIELDS + SIZE_FIELDS))
                writer.writeheader()
                from datetime import datetime, timezone
                for market, rows in grouped.items():
                    for row in rows:
                        values = dict(zip(PRICE_FIELDS + SIZE_FIELDS, row["values"]))
                        writer.writerow(dict(values, slug=market, ts_iso=datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
                                              final_price="future-field-must-be-ignored"))
            p["files"] = [{"path": "quotes.csv", "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
            result = run(root, p)
            self.assertEqual(result["diagnostic"]["coverage"]["validation"]["labeled_windows"], 3)
            self.assertTrue(result["raw_unchanged"])
            self.assertEqual(result["real_fills"], 0)
            self.assertIsNone(result["pnl"])
            path.write_text(path.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "hash_mismatch"):
                load_frozen_quotes(root, p)


if __name__ == "__main__":
    unittest.main()
