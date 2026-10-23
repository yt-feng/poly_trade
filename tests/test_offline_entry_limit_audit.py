import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from analysis import offline_entry_limit_audit as audit


def candidate_fixture():
    start = pd.Timestamp("2026-01-02T00:00:00Z")
    slug = "btc-updown-5m-" + str(int(start.timestamp()))
    return {"market_id": "new::" + slug, "run_name": "new", "slug": slug, "side": "up",
        "slippage": "0.01", "entry_ts": (start + pd.Timedelta(seconds=124)).isoformat(),
        "decision_ts": (start + pd.Timedelta(seconds=120)).isoformat(),
        "market_close_ts": (start + pd.Timedelta(seconds=300)).isoformat(),
        "label_observed_ts": "", "terminal_proxy_observed_ts": "", "terminal_proxy_gap_seconds": "",
        "outcome_up": "", "q_side": "0.9", "price": "0.8", "fill_price": "0.81",
        "sell_up_size_2m": "1000", "sell_down_size_2m": "1000"}


def config_fixture():
    rows = [candidate_fixture()]
    return {"protocol_id": "test", "source_commit": "a" * 40,
        "expected_code_commit": "b" * 40, "parent_code_commit": "c" * 40,
        "parent_summary_sha256": "d" * 64, "parent_candidate_csv_sha256": "e" * 64,
        "features": ["btc_move_1m"], "decision_cutoff_seconds": 120, "entry_delay_seconds": 4,
        "maximum_snapshot_age_seconds": 90, "model_params": {"random_state": 1},
        "confidence_floor": .8, "favorite_price_floor": .5, "depth_thresholds": [3, 5, 7],
        "slippages": [.01], "latencies_seconds": [2], "depth_haircuts": [.1],
        "minimum_training_runs": 1, "evaluation_block_runs": 12,
        "terminal_proxy_max_gap_seconds": 1, "cash_release_delay_after_close_seconds": 900,
        "evaluation_mode": "paired_entry_limit", "entry_edge_floor": .02,
        "control_reference_results_sha256": "0" * 64,
        "control_reference_candidate_counts": {"0.01": 1},
        "run_order": ["old", "new"], "test_runs": ["new"], "evaluation_blocks": [["new"]],
        "max_candidate_rows": 10, "max_replay_cells": 4,
        "frozen_candidates": rows, "frozen_candidate_rows_sha256": audit.candidate_rows_sha256(rows)}


def quote_fixture():
    start = pd.Timestamp("2026-01-02T00:00:00Z")
    return pd.DataFrame({"ts_utc": [start + pd.Timedelta(seconds=126)],
        "buy_up_cents": [90.], "buy_down_cents": [12.], "sell_up_cents": [88.],
        "sell_down_cents": [10.], "buy_up_size": [1000.], "buy_down_size": [1000.],
        "sell_up_size": [1000.], "sell_down_size": [1000.]})


class OfflineEntryLimitAuditTests(unittest.TestCase):
    def test_ignored_source_csv_is_not_part_of_the_pinned_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                               stderr=subprocess.DEVNULL).strip()
            git("init")
            (root / ".gitignore").write_text("ignored.csv\n")
            git("add", ".gitignore")
            git("-c", "user.name=Contract", "-c", "user.email=contract@example.invalid",
                "commit", "-m", "fixture")
            (root / "ignored.csv").write_text("unbound quote payload\n")
            self.assertEqual(git("ls-files", "--others", "--exclude-standard"), "")
            config = config_fixture()
            config["source_commit"] = git("rev-parse", "HEAD")
            config["expected_code_commit"] = subprocess.check_output(
                ["git", "-C", str(audit.CURRENT_DIR), "rev-parse", "HEAD"], text=True).strip()
            with self.assertRaisesRegex(ValueError, "source_archive_untracked_files"):
                audit.verified_commits(root, config)

    def test_candidate_hash_checked_before_conversion_and_empty_label_stays_unknown(self):
        config = config_fixture()
        audit.validate_config(config)
        frame, counts = audit.frozen_candidates(config)
        self.assertEqual(counts, {"0.01": 1})
        self.assertTrue(pd.isna(frame.iloc[0].outcome_up))
        self.assertTrue(pd.isna(frame.iloc[0].label_observed_ts))
        config["frozen_candidates"][0]["q_side"] = "0.91"
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            audit.frozen_candidates(config)

    def test_rejects_extra_fields_duplicate_identity_and_changed_clock(self):
        for kind, expected in (("future", "projection"), ("duplicate", "duplicate"), ("clock", "clock")):
            with self.subTest(kind=kind):
                config = config_fixture()
                if kind == "future":
                    config["frozen_candidates"][0]["future_pnl"] = "1"
                elif kind == "duplicate":
                    config["frozen_candidates"] *= 2
                else:
                    config["frozen_candidates"][0]["entry_ts"] = "2026-01-02T00:02:05Z"
                config["frozen_candidate_rows_sha256"] = audit.candidate_rows_sha256(config["frozen_candidates"])
                with self.assertRaisesRegex(ValueError, expected):
                    audit.frozen_candidates(config)

    def test_partition_and_budget_cannot_silently_change(self):
        for key, value in (("evaluation_blocks", []), ("test_runs", ["old"]), ("max_replay_cells", 3),
                           ("latencies_seconds", [2, 2])):
            with self.subTest(field=key):
                config = config_fixture(); config[key] = value
                with self.assertRaises(ValueError):
                    audit.validate_config(config)

    def test_same_candidates_control_enters_treatment_cancels_before_debit(self):
        config = config_fixture()
        frame, _ = audit.frozen_candidates(config)
        quotes = {frame.iloc[0].market_id: quote_fixture()}
        control = audit.replay_arm(frame, quotes, config, False)[0]
        treatment = audit.replay_arm(frame, quotes, config, True)[0]
        self.assertEqual(control["fills"], 1)
        self.assertEqual(control["chain"][0]["status"], "unresolved_position")
        self.assertLess(control["ending_cash"], 1e-9)
        self.assertGreater(control["locked_shares"], 0)
        self.assertEqual(treatment["fills"], 0)
        self.assertEqual(treatment["ending_cash"], 10.)
        self.assertEqual(treatment["attempts"][0]["status"], "entry_cost_limit_exceeded")

    def test_quote_loader_keeps_chosen_run_and_excludes_conflicting_duplicates(self):
        config = config_fixture(); frame, _ = audit.frozen_candidates(config)
        market = frame.iloc[0].market_id
        raw = quote_fixture().rename(columns={"ts_utc": "ts_iso"})
        raw["slug"] = frame.iloc[0].slug
        raw["final_price"] = 100.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "new").mkdir(); (root / "excluded").mkdir()
            path = root / "new" / "part1_btc-updown-5m_quotes.csv"
            raw.to_csv(path, index=False)
            raw.assign(buy_up_cents=40.).to_csv(root / "excluded" / path.name, index=False)
            index, coverage = audit.read_candidate_quotes(root, frame, config)
            self.assertEqual(coverage["scanned_files"], 1)
            self.assertEqual(index[market].iloc[0].buy_up_cents, 90.)
            raw.assign(final_price=101.).to_csv(root / "new" / "part2_btc-updown-5m_quotes.csv", index=False)
            index, coverage = audit.read_candidate_quotes(root, frame, config)
            self.assertNotIn(market, index)
            self.assertEqual(coverage["conflicting_quote_markets_excluded"], [market])

    def test_full_control_fingerprint_required_and_no_model_calls(self):
        for matches in (True, False):
            with self.subTest(matches=matches):
                config = config_fixture(); frame, _ = audit.frozen_candidates(config)
                quotes = {frame.iloc[0].market_id: quote_fixture()}
                if matches:
                    config["control_reference_results_sha256"] = audit.paired.results_fingerprint(audit.replay_arm(frame, quotes, config, False))
                with tempfile.TemporaryDirectory() as temp, \
                     patch.object(audit, "verified_commits", return_value=("a" * 40, "b" * 40)), \
                     patch.object(audit, "read_candidate_quotes", return_value=(quotes, {})), \
                     patch.object(audit.base, "prepare_archive", side_effect=AssertionError("must not rebuild features")), \
                     patch.object(audit.paired, "ExtraTreesClassifier", side_effect=AssertionError("must not fit")):
                    result = audit.evaluate(Path(temp), config, Path(temp) / "output")
                    self.assertTrue(result["strategy_evaluated"])
                    self.assertTrue(result["replay_evaluated"])
                    self.assertEqual(result["candidate_rows_reused"], 1)
                    self.assertEqual(result["replayed_cells"], 4)
                    self.assertEqual(result["actual_model_fit_calls"], 0)
                    self.assertEqual(result["actual_model_predict_calls"], 0)
                    self.assertEqual(result["causal_comparison_valid"], matches)
                    self.assertEqual(result["evaluation_status"], "evaluated" if matches else "evaluated_control_mismatch")
                    for path in (Path(temp) / "output").iterdir():
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_source_and_code_binding_fail_before_replay(self):
        config = config_fixture()
        with patch.object(audit.subprocess, "check_output", side_effect=["a" * 40, "f" * 40]):
            with self.assertRaisesRegex(ValueError, "commit_mismatch"):
                audit.verified_commits(Path("."), config)

    def test_source_untracked_and_code_working_changes_are_not_covered_by_head(self):
        config = config_fixture()
        with patch.object(audit.subprocess, "check_output", side_effect=["a" * 40, "b" * 40, "new/extra_quotes.csv\n"]), \
             patch.object(audit.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(ValueError, "source_archive_untracked_files"):
                audit.verified_commits(Path("."), config)
        with patch.object(audit.subprocess, "check_output", side_effect=["a" * 40, "b" * 40, ""]), \
             patch.object(audit.subprocess, "run", side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]):
            with self.assertRaisesRegex(ValueError, "executed_code_tracked_changes"):
                audit.verified_commits(Path("."), config)


if __name__ == "__main__":
    unittest.main()
