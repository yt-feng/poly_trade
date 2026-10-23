import copy
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis import archived_quote_cash_domain_export as audit


def base_config():
    runs = ["past", "test"]
    return {"protocol_id": "synthetic_base", "evaluation_mode": "book_bid_residual",
        "source_commit": "a" * 40, "expected_code_commit": "b" * 40,
        "features": audit.base.FEATURES.copy(), "source_runs": runs, "run_order": runs,
        "expected_files_per_run": 1, "minimum_prefix_quotes": 3, "minimum_training_runs": 1,
        "minimum_training_labels": 1, "evaluation_block_runs": 12, "max_source_rows": 1000,
        "max_feature_rows": 100, "max_scored_runs": 10, "max_replay_cells": 16,
        "opening_tolerance_seconds": 3., "maximum_snapshot_age_seconds": 3.,
        "decision_cutoff_seconds": 120., "entry_delay_seconds": 4., "hold_seconds": 30.,
        "latency_seconds": 8., "max_quote_delay_seconds": 3., "edge_floor": .02,
        "fee_rate": .07, "slippage": .01, "depth_haircut": .1,
        "minimum_order_usd": 5., "minimum_order_shares": 5., "depth_thresholds": [3., 5., 7.],
        "cash_release_delay_after_close_seconds": 900., "ridge_alpha": 1., "ridge_solver": "svd",
        "initial_cash": 10., "required_fills": 10}


def row_and_book(side="up", offset=0):
    start = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(seconds=offset)
    row = pd.Series({"market_id": f"m{offset}", "run_name": "test", "slug": f"s{offset}", "side": side,
        "decision_ts": start, "market_close_ts": start + pd.Timedelta(seconds=180),
        "target_status": "known_future_bid", "label_available_ts": start + pd.Timedelta(seconds=50),
        "future_bid_price": .6})
    snapshots = []
    for seconds in (12, 13, 50, 51):
        tick = {"ts_utc": start + pd.Timedelta(seconds=seconds), "quote_conflict": False}
        tick.update({"buy_up_cents": 40., "sell_up_cents": 39., "buy_down_cents": 60., "sell_down_cents": 59.,
            "buy_up_size": 400., "sell_up_size": 300., "buy_down_size": 200., "sell_down_size": 150.})
        if seconds >= 50:
            tick.update(buy_up_cents=61., sell_up_cents=60., buy_down_cents=41., sell_down_cents=40.)
        snapshots.append(tick)
    return row, pd.DataFrame(snapshots)


def synthetic_inputs():
    parent = base_config()
    first, first_book = row_and_book()
    first["run_name"] = "past"
    second, second_book = row_and_book("down", 300)
    third, _ = row_and_book(offset=600)
    third["future_bid_price"] = np.nan
    third["label_available_ts"] = pd.NaT
    third["target_status"] = "entry_no_quote_in_window"
    frame = pd.DataFrame([first, second, third]).reset_index(drop=True)
    quotes = {first.market_id: first_book, second.market_id: second_book}
    cohort = frame[frame.run_name.eq("test")]
    coverage = {"admitted_markets": len(frame), "source_rows": 8, "clock_basis": "synthetic"}
    config = {"protocol_id": "synthetic_export", "evaluation_mode": "archived_quote_cash_domain_export",
        "source_commit": "a" * 40, "expected_code_commit": "c" * 40, "parent_code_commit": "d" * 40,
        "base_protocol": parent, "base_protocol_sha256": audit.digest(parent),
        "export_contract": {"version": "archived_quote_cash_domain_v2", "columns": audit.COLUMNS.copy(),
            "selected_raw_snapshot_fields": {"base_columns": audit.base.BOOK_NUMERIC.copy(),
                "exported_columns": audit.RAW_COLUMNS.copy()}},
        "expected_coverage_sha256": audit.digest(coverage), "expected_all_cohort": audit.cohort_contract(frame),
        "expected_scored_cohort": audit.cohort_contract(cohort),
        "expected_scored_cohort_per_run": {"test": audit.cohort_contract(cohort)}, "expected_test_runs": ["test"],
        "unchanged_code_file_sha256": {name: audit.file_sha256(audit.CURRENT_DIR.parent / name) for name in audit.UNCHANGED_FILES},
        "environment": {"python_major_minor": "3.11", "packages": {name: "1.0" for name in audit.parent_audit.PACKAGE_MODULES},
            "must_assert_effective_get_params_exact_before_fit": True,
            "must_assert_runtime_versions_before_model_construction": True,
            "must_record_loaded_histogram_implementation_module_file_hashes": True},
        "max_new_model_fit_calls": 0, "max_new_model_predict_calls": 0, "max_replay_cells": 0,
        "max_new_cloud_runs": 1, "max_private_config_bytes": 262144, "actual_fills": 0,
        "verified_canary_fills": 0, "live_orders_enabled": False, "new_data_collection_allowed": False}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "features.csv"
        audit.base.storage._private_csv(path, frame)
        config["expected_features_csv_sha256"] = audit.file_sha256(path)
    return config, frame, quotes, coverage


class QuoteDomainRowTests(unittest.TestCase):
    def assert_unknown_domain(self, result):
        self.assertIs(result["simulation_cash_domain_observed"], False)
        for field in audit.DERIVED_COLUMNS:
            self.assertIsNone(result[field], field)

    def test_units_both_sides_minimum_and_cash_clock(self):
        for side, ask, bid, size_in, size_out in [("up", .4, .6, 400, 300), ("down", .6, .4, 200, 150)]:
            with self.subTest(side=side):
                row, book = row_and_book(side)
                result = audit.export_quote_row(row, book, base_config())
                p_in, p_out = ask + .01, bid - .01
                c_in = p_in + .07*p_in*(1-p_in)
                c_out = p_out - .07*p_out*(1-p_out)
                self.assertIs(result["simulation_cash_domain_observed"], True)
                self.assertEqual(list(result), audit.COLUMNS)
                self.assertEqual(result["entry_ask_price"], ask)
                self.assertEqual(result["exit_bid_price"], bid)
                self.assertAlmostEqual(result["entry_cash_cost_per_share"], c_in)
                self.assertAlmostEqual(result["exit_net_proceeds_per_share"], c_out)
                self.assertAlmostEqual(result["net_cash_difference_per_share"], c_out-c_in)
                self.assertAlmostEqual(result["minimum_entry_shares"], max(5., 5./p_in))
                self.assertAlmostEqual(result["minimum_entry_cash"], max(5., 5./p_in)*c_in)
                self.assertEqual(result["maximum_pair_capacity_shares"], min(size_in, size_out)*.1)
                self.assertEqual(result["cash_domain_label_available_ts"], result["exit_quote_ts"])
                self.assertEqual(pd.Timestamp(result["assumed_cash_release_ts"]), row.market_close_ts + pd.Timedelta(seconds=900))
                self.assertIn("not_authenticated", result["cash_release_basis"])

    def test_actual_entry_arrival_sets_exit_target(self):
        row, book = row_and_book()
        book = book[book.ts_utc.ne(row.decision_ts + pd.Timedelta(seconds=12))]
        result = audit.export_quote_row(row, book, base_config())
        self.assertEqual(result["entry_quote_delay_seconds"], 1.)
        self.assertEqual(pd.Timestamp(result["exit_target_ts"]), row.decision_ts + pd.Timedelta(seconds=51))
        self.assertEqual(result["exit_quote_delay_seconds"], 0.)

    def test_first_selected_invalid_snapshot_is_not_replaced(self):
        mutations = [(0, "buy_up_size", -1.), (0, "buy_up_size", np.nan),
            (0, "sell_down_size", -1.), (0, "sell_down_size", "broken"),
            (0, "sell_down_cents", 70.), (2, "sell_up_size", -1.),
            (2, "sell_up_size", np.inf), (2, "buy_down_cents", 1.)]
        for index, field, value in mutations:
            with self.subTest(index=index, field=field, value=value):
                row, book = row_and_book()
                book[field] = book[field].astype(object)
                book.at[index, field] = value
                result = audit.export_quote_row(row, book, base_config())
                self.assert_unknown_domain(result)
                kind = "entry" if index == 0 else "exit"
                self.assertEqual(result[kind + "_quote_ts"], book.at[index, "ts_utc"].isoformat())
                self.assertIs(result[kind + "_full_book_valid"], False)
                self.assertIs(result["paired_prices_observed"], True)
                if value == -1.:
                    self.assertEqual(result[kind + "_" + field], -1.)
                elif isinstance(value, str) or not np.isfinite(value):
                    self.assertIsNone(result[kind + "_" + field])

    def test_zero_depth_is_observed_domain_and_false_capacity(self):
        for index, field in [(0, "buy_up_size"), (2, "sell_up_size")]:
            with self.subTest(field=field):
                row, book = row_and_book()
                book.at[index, field] = 0.
                result = audit.export_quote_row(row, book, base_config())
                self.assertIs(result["simulation_cash_domain_observed"], True)
                self.assertIs(result["minimum_order_has_pair_capacity"], False)
                self.assertEqual(result["maximum_pair_capacity_shares"], 0.)
                self.assertGreater(result["net_cash_difference_per_share"], 0.)

    def test_minimum_size_capacity_boundary(self):
        row, book = row_and_book()
        minimum = 5./.41
        for deficit, expected in [(0., True), (1e-6, False)]:
            with self.subTest(deficit=deficit):
                book.at[2, "sell_up_size"] = (minimum-deficit)/.1
                result = audit.export_quote_row(row, book, base_config())
                self.assertIs(result["minimum_order_has_pair_capacity"], expected)

    def test_invalid_post_slippage_does_not_clip(self):
        for index, field, value in [(0, "buy_up_cents", 99.), (2, "sell_up_cents", 1.)]:
            with self.subTest(field=field):
                row, book = row_and_book()
                book.at[index, field] = value
                result = audit.export_quote_row(row, book, base_config())
                self.assertIs(result["paired_high_quality_quotes"], True)
                self.assert_unknown_domain(result)
                kind = "entry" if index == 0 else "exit"
                self.assertEqual(result[kind + "_" + field], value)

    def test_missing_entry_exit_and_conflict_keep_distinct_unknown_reasons(self):
        row, book = row_and_book()
        missing = audit.export_quote_row(row, None, base_config())
        self.assertEqual(missing["entry_quote_status"], "missing_book")
        self.assertEqual(missing["exit_quote_status"], "not_reached_entry_quote_unknown")
        self.assert_unknown_domain(missing)
        for kind in ("entry", "exit"):
            self.assertIsNone(missing[kind + "_full_book_valid"])
            self.assertIsNone(missing[kind + "_size_valid"])
        for index, kind in [(0, "entry"), (2, "exit")]:
            with self.subTest(kind=kind):
                conflicted = book.copy()
                conflicted.at[index, "quote_conflict"] = True
                result = audit.export_quote_row(row, conflicted, base_config())
                self.assertEqual(result[kind + "_quote_status"], "unknown_quote_conflict")
                self.assertIsNone(result[kind + "_quote_ts"])
                self.assert_unknown_domain(result)
        result = audit.export_quote_row(row, book.iloc[:2], base_config())
        self.assertEqual(result["exit_quote_status"], "no_quote_in_window")
        self.assertIs(result["entry_full_book_valid"], True)
        self.assertIsNone(result["exit_full_book_valid"])
        self.assert_unknown_domain(result)

    def test_frozen_selector_skips_only_invalid_price_not_size(self):
        row, book = row_and_book()
        book.at[0, "buy_up_cents"] = np.nan
        result = audit.export_quote_row(row, book, base_config())
        self.assertEqual(result["entry_quote_delay_seconds"], 1.)
        self.assertEqual(result["exit_quote_delay_seconds"], 0.)
        self.assertIs(result["simulation_cash_domain_observed"], True)


class ExportContractTests(unittest.TestCase):
    def run_export(self, config, frame, quotes, coverage, *, source_failure=None, runtime_failure=None, export_failure=None):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            spies = {}
            for module, name in [(audit.base, "fit_predict_once"), (audit.base, "Ridge"),
                    (audit.parent_audit, "HistGradientBoostingRegressor"), (audit.parent_audit, "fit_predict_once"),
                    (audit.parent_audit, "replay_arm"), (audit.parent_audit, "build_fit_plan")]:
                spies[name + str(id(module))] = stack.enter_context(patch.object(module, name, side_effect=AssertionError("forbidden computation")))
            verify = stack.enter_context(patch.object(audit, "verified_inputs", side_effect=source_failure,
                return_value={"source_commit": config["source_commit"], "code_commit": config["expected_code_commit"]}))
            runtime = stack.enter_context(patch.object(audit, "assert_runtime_environment", side_effect=runtime_failure, return_value={"synthetic": True}))
            stack.enter_context(patch.object(audit.parent_audit, "implementation_module_hashes", return_value=[]))
            prepare = stack.enter_context(patch.object(audit.base, "prepare_archive", return_value=(frame, quotes, coverage)))
            exporter = stack.enter_context(patch.object(audit, "quote_export_frame", wraps=audit.quote_export_frame,
                side_effect=export_failure))
            output = Path(folder) / "result"
            report = audit.evaluate(Path(folder) / "never_read", config, output)
            saved = json.loads((output / "summary.json").read_text())
            self.assertEqual(saved["evidence_exported"], report["evidence_exported"])
            self.assertEqual((output / "summary.json").stat().st_mode & 0o777, 0o600)
            for spy in spies.values():
                spy.assert_not_called()
            for name in ("actual_model_fit_calls", "actual_model_predict_calls", "replayed_cells", "actual_fills", "verified_canary_fills"):
                self.assertEqual(saved[name], 0)
            self.assertIs(saved["strategy_evaluated"], False)
            path = output / "quote-cash-domains.csv"
            exported = pd.read_csv(path) if path.exists() else None
            return report, exported, {"verify": verify.call_count, "runtime": runtime.call_count,
                "prepare": prepare.call_count, "export": exporter.call_count}

    def test_full_export_same_rows_order_unknown_retained_and_zero_compute(self):
        config, frame, quotes, coverage = synthetic_inputs()
        report, exported, calls = self.run_export(config, frame, quotes, coverage)
        self.assertEqual(report["export_status"], "complete")
        self.assertTrue(report["evidence_exported"])
        self.assertTrue(report["all_identity_gates_passed"])
        self.assertEqual(report["exported_rows"], len(frame))
        self.assertEqual(exported.market_id.tolist(), frame.market_id.tolist())
        self.assertEqual(list(exported), audit.COLUMNS)
        self.assertEqual(report["artifacts"]["features.csv"]["sha256"], config["expected_features_csv_sha256"])
        self.assertIn("coverage.json", report["artifacts"])
        self.assertTrue(pd.isna(exported.iloc[2].minimum_order_has_pair_capacity))
        self.assertEqual(calls, {"verify": 1, "runtime": 1, "prepare": 1, "export": 1})

    def test_all_identity_gates_precede_export(self):
        mutations = [lambda c: c.update(expected_features_csv_sha256="0"*64),
            lambda c: c.update(expected_coverage_sha256="0"*64),
            lambda c: c["expected_all_cohort"].update(scored_ids_sha256="0"*64),
            lambda c: c["expected_scored_cohort"].update(known_ids_sha256="0"*64),
            lambda c: c["expected_scored_cohort_per_run"]["test"].update(unknown_ids_sha256="0"*64)]
        for mutate in mutations:
            with self.subTest(mutate=mutations.index(mutate)):
                config, frame, quotes, coverage = synthetic_inputs()
                mutate(config)
                report, exported, calls = self.run_export(config, frame, quotes, coverage)
                self.assertFalse(report["evidence_exported"])
                self.assertFalse(report["all_identity_gates_passed"])
                self.assertEqual(calls["export"], 0)
                self.assertIsNone(exported)
                self.assertEqual(report["exported_rows"], 0)
                self.assertIn("features.csv", report["artifacts"])

    def test_configuration_failures_precede_source_read(self):
        mutations = [lambda c: c["export_contract"].update(version="archived_quote_cash_domain_v1"),
            lambda c: c["export_contract"]["columns"].pop(),
            lambda c: c["base_protocol"].update(slippage=.02),
            lambda c: c.update(max_new_model_fit_calls=True),
            lambda c: c.update(max_replay_cells=1),
            lambda c: c["unchanged_code_file_sha256"].pop(next(iter(c["unchanged_code_file_sha256"])))]
        for mutate in mutations:
            with self.subTest(mutate=mutations.index(mutate)):
                config, frame, quotes, coverage = synthetic_inputs()
                mutate(config)
                report, _, calls = self.run_export(config, frame, quotes, coverage)
                self.assertFalse(report["evidence_exported"])
                self.assertEqual(calls, {"verify": 0, "runtime": 0, "prepare": 0, "export": 0})

    def test_source_and_runtime_failure_precede_archive_read(self):
        for kwargs in [{"source_failure": ValueError("source_mismatch")}, {"runtime_failure": ValueError("runtime_mismatch")}]:
            with self.subTest(kwargs=kwargs):
                report, _, calls = self.run_export(*synthetic_inputs(), **kwargs)
                self.assertFalse(report["evidence_exported"])
                self.assertEqual(calls["prepare"], 0)
                self.assertEqual(calls["export"], 0)

    def test_frozen_module_byte_mismatch_rejected(self):
        config, _, _, _ = synthetic_inputs()
        first = next(iter(config["unchanged_code_file_sha256"]))
        config["unchanged_code_file_sha256"][first] = "0"*64
        with patch.object(audit.base.integrity, "verified_commits", return_value=("a"*40, "c"*40)):
            with self.assertRaisesRegex(ValueError, "frozen_code_file"):
                audit.verified_inputs(Path("unused"), config)

    def test_export_failure_preserves_partial_artifact_evidence(self):
        report, exported, calls = self.run_export(*synthetic_inputs(), export_failure=ValueError("synthetic_export_failure"))
        self.assertFalse(report["evidence_exported"])
        self.assertEqual(report["export_status"], "incomplete")
        self.assertEqual(report["exported_rows"], 0)
        self.assertEqual(report["failure"]["stage"], "all_row_quote_export")
        self.assertTrue(report["all_identity_gates_passed"])
        self.assertIn("features.csv", report["artifacts"])
        self.assertNotIn("quote-cash-domains.csv", report["artifacts"])
        self.assertIsNone(exported)

    def test_duplicate_identity_and_existing_output_rejected(self):
        config, frame, quotes, coverage = synthetic_inputs()
        with self.assertRaisesRegex(ValueError, "row_identity"):
            audit.quote_export_frame(pd.concat([frame, frame.iloc[:1]]), quotes, config["base_protocol"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "keep").write_text("original")
            with self.assertRaisesRegex(ValueError, "must_be_empty"):
                audit.evaluate(path, config, path)
            self.assertEqual((path / "keep").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
