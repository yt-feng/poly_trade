import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from analysis import archive_coverage_ledger as ledger
from analysis import historical_walk_forward as walk
from analysis import quote_factor_diagnostic as original
from tests.test_archive_coverage_ledger import DATE, START, observation, write_csv
from tests.test_quote_factor_diagnostic import protocol


BINS = [0, 25, 50, 75, 100]


def config():
    result = protocol()
    result.update(train_dates=[DATE], validation_dates=["2026-09-27"])
    return result


def quote(start, offset, level=50, *, depth=True, valid=True):
    return {"ts": start + offset, "values": (level + 1, level, 50, 49) + ((10, 11, 12, 13) if depth else (None,) * 4),
            "price_reason": None if valid else "missing_or_nonfinite_price",
            "depth_reason": None if depth else "missing_or_nonfinite_depth"}


def rows_for(start=START):
    return [quote(start, 104, 49), quote(start, 119, 50), quote(start, 150, 52)]


def sample(rows=None, *, start=START, cohort="common", **kwargs):
    return walk.sample_window(rows if rows is not None else rows_for(start), f"btc-updown-5m-{start}",
                              start, datetime.fromtimestamp(start, timezone.utc).date().isoformat(),
                              config(), cohort, BINS, True, **kwargs)


def small_samples(cohort="common"):
    rows = []
    for day in (0, 1):
        for i in range(3):
            start = START + day * 86400 + i * 300
            observations = [quote(start, 104, 30 + i), quote(start, 119, 31 + 2 * i),
                            quote(start, 150, 32 + 3 * i)]
            rows.append(sample(observations, start=start, cohort=cohort))
    return rows


def folds():
    return [{"train_dates": [DATE], "validation_dates": ["2026-09-27"]}]


class HistoricalWalkForwardTests(unittest.TestCase):
    def test_common_retains_absent_depth_without_imputing_imbalance(self):
        raw = rows_for()
        raw[1] = quote(START, 119, 50, depth=False)
        common = sample(raw)
        self.assertEqual(common["status"], "labeled_quote_change")
        self.assertEqual(set(common["features"]), {"momentum", "spread"})
        depth = sample(raw, cohort="depth")
        self.assertEqual(depth["status"], "excluded_decision_depth")
        self.assertIsNone(depth["features"])
        raw = rows_for()
        raw[0] = quote(START, 104, 49, depth=False)
        self.assertEqual(sample(raw)["status"], "labeled_quote_change")
        self.assertEqual(sample(raw, cohort="depth")["status"], "excluded_lookback_depth")

    def test_future_depth_is_not_price_unknown_and_legacy_rule_is_separate(self):
        raw = rows_for()
        raw[-1] = quote(START, 150, 52, depth=False)
        for cohort in ("common", "depth"):
            current = sample(raw, cohort=cohort)
            self.assertEqual(current["status"], "labeled_quote_change")
            self.assertEqual(current["targets"], {"bid_change_cents": 2, "ask_change_cents": 2})
        legacy = sample(raw, cohort="legacy_depth", require_future_depth=True)
        self.assertEqual(legacy["status"], "excluded_future_depth_legacy_rule")
        self.assertTrue(legacy["future_price_observed"])
        self.assertIsNone(legacy["targets"])
        coverage = walk.coverage([legacy])
        self.assertEqual(coverage["unknown_future_quotes"], 0)
        self.assertEqual(coverage["legacy_future_depth_exclusions"], 1)
        legacy["predictions"] = {"bid_change_cents": {"zero": 0, "train_mean": 0, "momentum": 0}}
        score = walk.metrics([legacy], "bid_change_cents", ("zero",))
        self.assertEqual(score["predicted_unscored_labels"], 1)
        self.assertEqual(score["predicted_unknown_future_quotes"], 0)
        self.assertEqual(score["predicted_legacy_depth_exclusions"], 1)

    def test_legacy_correction_reproduces_original_features_and_target_cohort(self):
        grouped, modern = {}, {}
        for day in (0, 1):
            for i in range(3):
                start = START + day * 86400 + i * 300
                rows = rows_for(start)
                if i == 1:
                    rows[-1] = quote(start, 150, 52, depth=False)
                elif i == 2:
                    rows[0] = quote(start, 104, 49, depth=False)
                slug = f"btc-updown-5m-{start}"
                modern[slug] = sample(rows, start=start, cohort="legacy_depth", require_future_depth=True)
                grouped[slug] = [dict(row, invalid_reason=row["price_reason"] or row["depth_reason"]) for row in rows]
        old = original.scheduled_samples(grouped, set(), config())
        for row in old:
            if row["market"] not in modern:
                self.assertEqual(row["status"], "absent_window")
                continue
            repaired = modern[row["market"]]
            self.assertEqual(row["features"], repaired["features"])
            self.assertEqual(row["targets"], repaired["targets"])
            self.assertEqual(row.get("feature_times"), repaired.get("feature_times"))
            self.assertEqual(row.get("target_time"), repaired.get("target_time"))

    def test_first_bad_future_price_cannot_be_replaced_by_later_quote(self):
        raw = rows_for()
        raw[-1] = quote(START, 150, valid=False)
        raw.append(quote(START, 151, 80))
        for cohort in ("common", "depth"):
            row = sample(raw, cohort=cohort)
            self.assertEqual(row["status"], "unknown_future_price")
            self.assertIsNone(row["targets"])
            self.assertIsNotNone(row["features"])

    def test_decision_quote_and_bin_use_only_strict_predecision_quote(self):
        raw = [quote(START, 104, 20), quote(START, 119, 24),
               quote(START, 120, 60), quote(START, 150, 85)]
        row = sample(raw)
        self.assertEqual(row["feature_times"], [START + 104, START + 119])
        self.assertEqual(row["features"]["momentum"], 4)
        self.assertEqual(row["quote_bin"], "25:50")
        self.assertEqual(row["targets"]["ask_change_cents"], 61)

    def test_depth_conflict_only_excludes_depth_and_price_conflict_excludes_both(self):
        self.assertEqual(sample(depth_conflict=True)["status"], "labeled_quote_change")
        self.assertEqual(sample(cohort="depth", depth_conflict=True)["status"], "excluded_depth_conflict")
        for cohort in ("common", "depth"):
            self.assertEqual(sample(cohort=cohort, price_conflict=True)["status"], "excluded_price_conflict")

    def test_validate_plan_rejects_reused_validation_and_nonfinite_bins(self):
        plan = {"diagnostic": config(), "folds": folds(), "quote_bin_edges_cents": BINS}
        walk.validate_plan(plan)
        repeated = copy.deepcopy(plan)
        repeated["folds"].append(copy.deepcopy(repeated["folds"][0]))
        with self.assertRaisesRegex(ValueError, "repeated_validation_market_dates"):
            walk.validate_plan(repeated)
        bad = copy.deepcopy(plan)
        bad["quote_bin_edges_cents"] = [0, float("nan"), 100]
        with self.assertRaisesRegex(ValueError, "invalid_quote_bins"):
            walk.validate_plan(bad)

    def test_validation_label_changes_do_not_change_features_or_training_fit(self):
        original = small_samples("depth")
        changed = copy.deepcopy(original)
        for row in changed:
            if row["date"] == "2026-09-27":
                row["targets"] = {target: -90 for target in config()["targets"]}
        a = walk.evaluate(original, folds(), config(), "depth")
        b = walk.evaluate(changed, folds(), config(), "depth")
        self.assertEqual(a["folds"][0]["fits"], b["folds"][0]["fits"])
        self.assertEqual([x["features"] for x in a["window_ledger"]], [x["features"] for x in b["window_ledger"]])
        self.assertEqual([x["predictions"] for x in a["window_ledger"]], [x["predictions"] for x in b["window_ledger"]])
        self.assertNotEqual(a["overall"]["bid_change_cents"]["models"]["zero"]["mse"],
                            b["overall"]["bid_change_cents"]["models"]["zero"]["mse"])

    def test_every_model_comparison_within_cohort_uses_same_training_and_scored_rows(self):
        for cohort in ("common", "depth"):
            result = walk.evaluate(small_samples(cohort), folds(), config(), cohort)
            if cohort == "common":
                self.assertTrue(all("imbalance" not in fields for fields in result["models"].values()))
            else:
                self.assertIn("all", result["models"])
            for target in config()["targets"]:
                metrics = result["overall"][target]
                self.assertEqual(metrics["scored_labels"], 3)
                for model in metrics["models"].values():
                    self.assertEqual({x["n_common_labels"] for x in model["paired_improvement_vs"].values()}, {3})
            self.assertEqual(result["folds"][0]["train_coverage"]["labeled_windows"], 3)

    def test_unknown_label_bounds_are_paired_and_keep_feature_eligible_denominator(self):
        known = sample()
        known["targets"] = {"bid_change_cents": 2}
        known["predictions"] = {"bid_change_cents": {"zero": 0, "train_mean": 1, "momentum": 1}}
        unknown = copy.deepcopy(known)
        unknown.update(targets=None, status="unknown_future_missing")
        result = walk.metrics([known, unknown], "bid_change_cents", ("zero", "train_mean", "momentum"))
        self.assertEqual(result["coverage"]["feature_eligible_windows"], 2)
        self.assertEqual(result["scored_labels"], 1)
        self.assertEqual(result["coverage"]["unknown_future_quotes"], 1)
        model = result["models"]["momentum"]
        self.assertEqual(model["mse"], 1)
        self.assertEqual(model["mse_eligible_lower"], 0.5)
        self.assertEqual(model["mse_eligible_upper"], 5101)
        delta = model["paired_improvement_vs"]["zero"]
        self.assertEqual(delta, {"observed_mean": 3, "eligible_lower": -99,
                                 "eligible_upper": 101, "n_common_labels": 1})

    def test_insufficient_training_preserves_coverage_and_null_scores(self):
        cfg = config()
        cfg["min_train_labels"] = 4
        result = walk.evaluate(small_samples(), folds(), cfg, "common")
        self.assertEqual(result["folds"][0]["status"], "insufficient_training_labels")
        self.assertEqual(result["folds"][0]["fits"], {})
        for target in cfg["targets"]:
            self.assertEqual(result["overall"][target]["coverage"]["labeled_windows"], 3)
            self.assertEqual(result["overall"][target]["scored_labels"], 0)
            self.assertTrue(all(x["mse"] is None for x in result["overall"][target]["models"].values()))
        self.assertTrue(all(x["predictions"] is None for x in result["window_ledger"]))

    def test_calendar_cohorts_do_not_borrow_future_quote_from_adjacent_market(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, database = base / "source", base / "private.sqlite"
            rows = [observation(offset=104), observation(offset=119),
                    observation(START + 300, offset=150)]
            write_csv(root, "adjacent.csv", rows)
            ledger.build(root, database)
            with ledger.connect_readonly(database) as db:
                samples = walk.samples_for_dates(db, [DATE], config(), BINS)
            for cohort, rows in samples.items():
                self.assertEqual(len(rows), 288)
                self.assertEqual(rows[0]["status"], "unknown_future_missing")
                self.assertIsNone(rows[0]["targets"])
                self.assertEqual(rows[1]["status"], "excluded_decision_missing_or_stale")
                self.assertEqual(walk.coverage(rows)["status_counts"]["absent_window"], 286)

    def test_synthetic_cli_end_to_end_is_private_frozen_and_non_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, database = base / "source", base / "private.sqlite"
            raw = []
            for day in (0, 1):
                for i in range(3):
                    start = START + day * 86400 + i * 300
                    for offset in (104, 119, 150):
                        raw.append(observation(start, offset=offset))
            write_csv(root, "synthetic.csv", raw)
            ledger.build(root, database)
            with ledger.connect_readonly(database) as db:
                inventory_digest = ledger.coverage_report(db)["inventory_sha256"]
            repository = Path(walk.__file__).resolve().parents[1]
            code = "analysis/historical_walk_forward.py"
            plan = {"diagnostic": config(), "folds": folds(), "quote_bin_edges_cents": BINS,
                    "database_sha256": walk.file_hash(database), "inventory_sha256": inventory_digest,
                    "code_sha256": {code: walk.file_hash(repository / code)}}
            protocol_path, output = base / "protocol.json", base / "private-result.json"
            protocol_path.write_text(json.dumps(plan))
            completed = subprocess.run([sys.executable, str(repository / code), "--database", str(database),
                                        "--poly-root", str(root), "--protocol", str(protocol_path),
                                        "--output", str(output)], cwd=repository, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "PRIVATE_WALK_FORWARD_COMPLETE; no empirical contents printed.")
            result = json.loads(output.read_text())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertTrue(result["source_bytes_unchanged"])
            self.assertEqual(result["real_fills"], 0)
            self.assertIsNone(result["pnl"])
            self.assertFalse(result["canary_gate_changed"])
            for cohort in result["cohorts"].values():
                self.assertEqual(cohort["overall"]["bid_change_cents"]["scored_labels"], 3)
                self.assertEqual(cohort["overall"]["bid_change_cents"]["coverage"]["scheduled_windows"], 288)
            bad = dict(plan, database_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "ledger_hash_changed"):
                walk.run(database, root, bad)
            bad = dict(plan, inventory_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "inventory_digest_changed"):
                walk.run(database, root, bad)


if __name__ == "__main__":
    unittest.main()
