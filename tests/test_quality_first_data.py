"""Meaningful selection/causality regressions for quality-first research."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from quality_first_data import (CausalQualityEngine, QualityPolicy, build_quality_dataset,
                                chronological_split, compact_snapshot, gold_training_rows,
                                load_verified_snapshots, mark_evaluation_scope, normalize_snapshot)


START = 1_800_000_000_000


def observation(second, window=0, minimum=3):
    start = START + window * 300_000
    now = start + second * 1000
    slug = "btc-updown-5m-" + str(start // 1000)
    price = 50_000 + second
    book = {"bid": 0.49, "ask": 0.51, "bid_size": 100, "ask_size": 100,
            "received_ms": now, "event_ms": now}
    source = {"price": price, "received_ms": now, "event_ms": now}
    return {"asset": "btc", "slug": slug, "sample_ms": now, "window_start_ms": start,
            "up_token_id": "u" + str(window), "down_token_id": "d" + str(window),
            "poly_valid": True, "binance_valid": True, "chainlink_valid": True,
            "sampler_lag_ms": 5, "up": dict(book), "down": dict(book),
            "binance": dict(source), "chainlink": dict(source),
            "microstructure": {"schema_version": 3, "asof_ms": now,
                "rules": {"slug": slug, "condition_id": "condition" + str(window),
                    "metadata_received_ms": now, "rules_sha256": "a" * 64,
                    "active": True, "closed": False, "accepting_orders": True,
                    "reference_kind": "chainlink_spot", "min_order_size": minimum,
                    "tick_size": 0.01, "fees": {"known": True, "rate": "0.07", "exponent": 1,
                        "model": "C_rate_p_one_minus_p_2026-09-23", "asset": "USDC"}},
                "valid": {"rules": True, "coinbase": True},
                "coinbase_ticker": {"received_ms": now, "event_ms": now, "payload": {"price": str(price)}},
                "poly_up": {"received_ms": now, "event_ms": now, "min_order_size": minimum, "tick_size": 0.01},
                "poly_down": {"received_ms": now, "event_ms": now, "min_order_size": minimum, "tick_size": 0.01}}}


def full_window(window=0):
    return [observation(second, window) for second in range(300)]


class CausalQualityTests(unittest.TestCase):
    def test_old_reference_rule_does_not_block_unrelated_candidates(self):
        row = observation(10, minimum=11)
        decision = CausalQualityEngine(required_features=()).update(row)
        self.assertTrue(decision["quality"]["decision_eligible"])
        self.assertEqual(decision["min_order_size"], 11)
        self.assertEqual(decision["min_order_size_unit"], "shares")
        self.assertEqual(decision["min_order_size_unit_source"], "producer_clob_book_field")

    def test_market_minimum_alone_does_not_invent_a_share_unit(self):
        row = observation(10)
        for side in ("up", "down"):
            row["microstructure"]["poly_" + side].pop("min_order_size")
        decision = normalize_snapshot(row)
        self.assertTrue(decision["quality"]["execution_quality"]["accepted"])
        self.assertIsNone(decision["min_order_size_unit"])
        self.assertIn("up_minimum_order_unit_unknown", decision["execution_metadata_unverified"])
        self.assertFalse(decision["execution_metadata_verified"])

    def test_market_and_book_minimums_with_different_units_are_not_compared(self):
        row = observation(10)
        row["microstructure"]["poly_up"]["min_order_size"] = 6
        decision = normalize_snapshot(row)
        self.assertTrue(decision["quality"]["execution_quality"]["accepted"])
        self.assertIn("up_book_market_minimum_numeric_difference", decision["execution_metadata_unverified"])
        self.assertEqual(decision["up"]["min_order_size"], 6)
        self.assertEqual(decision["up"]["min_order_size_unit"], "shares")

    def test_future_received_spot_cannot_enter_features(self):
        engine = CausalQualityEngine(required_features=("spot_return_5s_bps",))
        for second in range(5):
            engine.update(observation(second))
        row = observation(5)
        row["binance"]["received_ms"] += 1000
        decision = engine.update(row)
        self.assertIsNone(decision["features"]["spot_return_5s_bps"])
        self.assertFalse(decision["quality"]["decision_eligible"])
        self.assertTrue(decision["quality"]["base_integrity"]["accepted"])
        self.assertTrue(decision["quality"]["execution_quality"]["accepted"])

    def test_feature_quality_only_requires_candidate_sources(self):
        row = observation(10)
        row["binance_valid"] = False
        book_only = CausalQualityEngine(required_features=()).update(row)
        spot = CausalQualityEngine(required_features=("spot_return_5s_bps",)).update(row)
        self.assertTrue(book_only["eligible"])
        self.assertFalse(spot["eligible"])
        self.assertTrue(spot["quality"]["execution_quality"]["accepted"])

    def test_bad_intermediate_source_breaks_lookback(self):
        engine = CausalQualityEngine(required_features=("spot_return_5s_bps",))
        result = None
        for second in range(11):
            row = observation(second)
            if second == 3:
                row["binance_valid"] = False
            result = engine.update(row)
            if second == 5:
                self.assertFalse(result["eligible"])
        self.assertTrue(result["eligible"])

    def test_missing_sampled_second_cuts_feature_history_without_filling(self):
        engine = CausalQualityEngine(required_features=("spot_return_5s_bps",))
        for second in range(11):
            if second == 3:
                continue
            decision = engine.update(observation(second))
            if second == 5:
                self.assertIsNone(decision["features"]["spot_return_5s_bps"])
            if second == 10:
                self.assertIsNotNone(decision["features"]["spot_return_5s_bps"])

    def test_future_gap_changes_training_selection_not_prior_decisions(self):
        rows = full_window()
        full = build_quality_dataset(rows)
        missing = build_quality_dataset([row for row in rows if row["sample_ms"] != START + 220_000])
        self.assertEqual(len(full["gold_window_ids"]), 1)
        self.assertEqual(len(missing["gold_window_ids"]), 1)
        earlier = [row for row in full["decisions"] if row["sample_ms"] <= START + 100_000]
        comparison = [row for row in missing["decisions"] if row["sample_ms"] <= START + 100_000]
        for before, after in zip(earlier, comparison):
            self.assertEqual(before["eligible"], after["eligible"])
            self.assertEqual(before["features"], after["features"])
            self.assertEqual(before["quality"], after["quality"])
        self.assertTrue(comparison[-1]["eligible"])
        self.assertEqual(missing["funnel"]["incomplete_windows_with_gold_segments"], 1)
        self.assertFalse(missing["decisions"][-1]["training_gold"])
        self.assertTrue(missing["decisions"][-1]["eligible"])

    def test_joint_quality_cannot_hide_one_bad_sample_in_same_second(self):
        rows = full_window()
        bad = observation(100)
        bad["sample_ms"] += 1
        bad["poly_valid"] = False
        rows.insert(101, bad)
        dataset = build_quality_dataset(rows, required_features=())
        self.assertEqual(dataset["windows"][0]["observed_seconds"], 300)
        self.assertEqual(dataset["windows"][0]["joint_good_seconds"], 299)
        self.assertTrue(dataset["windows"][0]["gold_for_training"])
        self.assertEqual(dataset["windows"][0]["gold_segment_count"], 2)
        self.assertFalse(dataset["decisions"][100]["training_gold"])
        self.assertFalse(dataset["decisions"][101]["training_gold"])

    def test_quality_funnel_separates_layers(self):
        good = observation(10)
        stale = observation(11)
        stale["up"]["received_ms"] -= 10_000
        dataset = build_quality_dataset([good, stale], required_features=())
        funnel = dataset["funnel"]
        self.assertEqual(funnel["layer_accepted_rows"]["base_integrity"], 2)
        self.assertEqual(funnel["layer_accepted_rows"]["execution_quality"], 1)
        self.assertEqual(funnel["layer_rejections"]["execution_quality"]["up_book_stale"], 1)

    def test_training_labels_must_be_available_at_fit_cutoff(self):
        dataset = build_quality_dataset(full_window())
        slug = dataset["gold_window_ids"][0]
        cutoff = START + 300_000
        self.assertEqual(gold_training_rows(dataset, cutoff, {slug: {"label_available_at_ms": cutoff + 1}}), [])
        self.assertGreater(len(gold_training_rows(dataset, cutoff, {slug: {"label_available_at_ms": cutoff}})), 0)

    def test_chronological_test_retains_incomplete_window_decisions(self):
        rows = sum((full_window(window) for window in range(6)), [])
        rows = [row for row in rows if row["sample_ms"] != START + 5 * 300_000 + 200_000]
        dataset = build_quality_dataset(rows)
        split = chronological_split(dataset, START + 600_000, START + 1_200_000, purge_ms=60_000)
        train_slugs = {row["slug"] for row in split["train"]}
        test_slugs = {row["slug"] for row in split["test"]}
        self.assertEqual(len(train_slugs), 2)
        self.assertEqual(len(test_slugs), 1)
        self.assertEqual(len(split["test"]), 299)
        self.assertTrue(test_slugs & set(dataset["gold_window_ids"]))
        self.assertTrue(any(not row["training_gold"] for row in split["test"]))
        self.assertFalse(train_slugs & test_slugs)

    def test_gold_segment_floor_is_fixed_and_no_missing_second_is_filled(self):
        short = build_quality_dataset([observation(second) for second in range(89)], required_features=())
        enough = build_quality_dataset([observation(second) for second in range(90)], required_features=())
        broken = build_quality_dataset([observation(second) for second in range(100) if second != 50], required_features=())
        self.assertEqual(short["gold_segments"], [])
        self.assertEqual(len(enough["gold_segments"]), 1)
        self.assertEqual(enough["gold_segments"][0]["observed_seconds"], 90)
        self.assertEqual(broken["gold_segments"], [])

    def test_non_monotonic_input_is_not_silently_reordered(self):
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            build_quality_dataset([observation(10), observation(9)])


class ArchiveQualityTests(unittest.TestCase):
    def archive(self, root, name, rows):
        path = Path(root) / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(path.name + ".sha256").write_text(digest + "  " + path.name + "\n")
        return path

    def test_identical_duplicates_dedupe_after_checksum_verification(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [observation(1), observation(2)]
            self.archive(root, "snapshots-1.jsonl.gz", rows)
            self.archive(root, "snapshots-2.jsonl.gz", rows)
            loaded, provenance = load_verified_snapshots(root)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(provenance["duplicate_rows"], 2)
            self.assertEqual([item["rows"] for item in provenance["files"]], [2, 2])
            self.assertEqual(provenance["files"][0]["first_sample_ms"], START + 1000)
            self.assertEqual(provenance["files"][0]["last_sample_ms"], START + 2000)
            self.assertEqual(provenance["memory_projection_version"], 1)

    def test_discarded_field_conflict_still_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            row = observation(1)
            changed = copy.deepcopy(row)
            changed["unneeded_diagnostics"] = "conflicting original"
            self.archive(root, "snapshots-1.jsonl.gz", [row])
            self.archive(root, "snapshots-2.jsonl.gz", [changed])
            with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
                load_verified_snapshots(root)

    def test_bad_checksum_fails_before_training(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.archive(root, "snapshots-1.jsonl.gz", [observation(1)])
            path.with_name(path.name + ".sha256").write_text("0" * 64)
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                load_verified_snapshots(root)

    def test_row_budget_does_not_silently_truncate_a_market(self):
        with tempfile.TemporaryDirectory() as root:
            self.archive(root, "snapshots-1.jsonl.gz", [observation(1), observation(2)])
            with self.assertRaisesRegex(ValueError, "ROW_BUDGET_EXCEEDED"):
                load_verified_snapshots(root, max_rows=1)

    def test_projection_preserves_causal_features_and_order_metadata(self):
        rows = [observation(second) for second in range(10)]
        for row in rows:
            row["microstructure"]["related_15m"] = {"large": "x" * 10000}
        raw_result = build_quality_dataset(rows)
        compact_result = build_quality_dataset([compact_snapshot(row) for row in rows])
        for raw, compact in zip(raw_result["decisions"], compact_result["decisions"]):
            self.assertEqual(raw["features"], compact["features"])
            self.assertEqual(raw["quality"], compact["quality"])
            self.assertEqual(raw["min_order_size_unit"], compact["min_order_size_unit"])
            self.assertNotIn("related_15m", compact["raw"]["microstructure"])

    def test_whole_file_bounds_exclude_only_partial_boundary_markets(self):
        rows = [observation(second, window) for window in range(3) for second in range(300)
                if START + 100_000 <= START + window * 300_000 + second * 1000 <= START + 800_000]
        with tempfile.TemporaryDirectory() as root:
            self.archive(root, "snapshots-1.jsonl.gz", rows)
            loaded, provenance = load_verified_snapshots(root)
            dataset = build_quality_dataset(loaded, required_features=())
            original_gold = [row["training_gold"] for row in dataset["decisions"]]
            returned = mark_evaluation_scope(dataset, provenance)
            self.assertIs(returned, dataset)
            self.assertEqual([window["evaluation_scope_included"] for window in dataset["windows"]], [False, True, False])
            self.assertEqual(dataset["evaluation_scope"]["excluded_boundary_market_windows"], 2)
            self.assertEqual(dataset["evaluation_scope"]["included_decisions"], 300)
            self.assertEqual(original_gold, [row["training_gold"] for row in dataset["decisions"]])

    def test_internal_missing_observations_never_cut_the_sampling_frame(self):
        rows = [observation(second, window) for window in range(3) for second in range(300)
                if not (window == 1 and 100 <= second < 200)]
        with tempfile.TemporaryDirectory() as root:
            self.archive(root, "snapshots-1.jsonl.gz", rows)
            loaded, provenance = load_verified_snapshots(root)
            dataset = mark_evaluation_scope(build_quality_dataset(loaded, required_features=()), provenance)
            self.assertEqual(len(dataset["evaluation_scope"]["frames"]), 1)
            self.assertEqual(dataset["evaluation_scope"]["included_market_windows"], 3)
            self.assertTrue(all(row["evaluation_scope_included"] for row in dataset["decisions"]))
            self.assertFalse(dataset["evaluation_scope"]["internal_gaps_split_frames"])

    def test_adjacent_file_frames_merge_without_interpreting_internal_quality(self):
        dataset = build_quality_dataset(full_window(), required_features=())
        provenance = {"files": [
            {"path": "first", "rows": 150, "first_sample_ms": START + 20, "last_sample_ms": START + 149_010},
            {"path": "second", "rows": 150, "first_sample_ms": START + 150_020, "last_sample_ms": START + 299_990},
        ]}
        mark_evaluation_scope(dataset, provenance)
        self.assertEqual(len(dataset["evaluation_scope"]["frames"]), 1)
        self.assertEqual(dataset["evaluation_scope"]["included_market_windows"], 1)
        self.assertEqual(dataset["evaluation_scope"]["frames"][0]["source_file_count"], 2)

    def test_separated_archive_frames_do_not_bridge_unselected_capture_time(self):
        dataset = build_quality_dataset(full_window(), required_features=())
        provenance = {"files": [
            {"path": "first", "rows": 100, "first_sample_ms": START, "last_sample_ms": START + 99_000},
            {"path": "second", "rows": 100, "first_sample_ms": START + 200_000, "last_sample_ms": START + 299_000},
        ]}
        mark_evaluation_scope(dataset, provenance)
        self.assertEqual(len(dataset["evaluation_scope"]["frames"]), 2)
        self.assertEqual(dataset["evaluation_scope"]["included_market_windows"], 0)
        self.assertTrue(all(row["training_gold"] for row in dataset["decisions"]))


if __name__ == "__main__":
    unittest.main()
