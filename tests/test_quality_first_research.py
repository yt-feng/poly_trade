"""Synthetic execution and causal-boundary checks; no market/account access."""
import copy
import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import quality_first_data as data
import quality_first_research as research


def raw_row(start, offset, bid=.49, ask=.50, rate=.07):
    ms = start + offset
    slug = f"btc-updown-5m-{start // 1000}"
    source = {"price": 100_000 + offset / 1000, "received_ms": ms, "event_ms": ms}
    book = {"bid": bid, "ask": ask, "bid_size": 100., "ask_size": 100.,
            "received_ms": ms, "event_ms": ms}
    detail = {"min_order_size": 5., "tick_size": .01, "received_ms": ms, "event_ms": ms}
    return {"asset": "btc", "slug": slug, "sample_ms": ms, "window_start_ms": start,
            "up_token_id": "up-" + str(start), "down_token_id": "down-" + str(start),
            "poly_valid": True, "sampler_lag_ms": 0,
            "up": copy.deepcopy(book), "down": copy.deepcopy(book),
            "binance": copy.deepcopy(source), "binance_valid": True,
            "chainlink": copy.deepcopy(source), "chainlink_valid": True,
            "microstructure": {"schema_version": 3, "asof_ms": ms,
                "valid": {"rules": True, "coinbase": True, "twap60": True},
                "coinbase_ticker": copy.deepcopy(source), "twap60": copy.deepcopy(source),
                "poly_up": copy.deepcopy(detail), "poly_down": copy.deepcopy(detail),
                "rules": {"slug": slug, "condition_id": "condition-" + str(start),
                    "metadata_received_ms": ms, "active": True, "closed": False,
                    "accepting_orders": True, "reference_kind": "chainlink_twap_60",
                    "min_order_size": 5., "tick_size": .01, "rules_sha256": "fixture",
                    "min_order_notional": 0., "order_size_step": 1.,
                    "fees": {"known": True, "rate": rate, "exponent": 1, "asset": "USDC",
                        "model": "C_rate_p_one_minus_p_2026-09-23",
                        "schedule": {"takerOnly": True}}}}}


def rows_for_window(start=0, last_offset=45_000, lift_at=32_000):
    return [raw_row(start, offset, * ((.59, .60) if offset >= lift_at else (.49, .50)))
            for offset in range(0, last_offset + 1, 1000)]


def decisions_for(rows):
    engine = data.CausalQualityEngine(required_features=())
    return [engine.update(row) for row in rows]


def fixed_model():
    # An intentionally simple synthetic signal, never fitted to real observations.
    return {"family": "book_residual", "coef": [.20, 0., 0., 0., 0., 0.]}


class CausalResearchTests(unittest.TestCase):
    def test_future_observation_changes_cannot_rewrite_prefix_features(self):
        rows = rows_for_window(last_offset=60_000)
        altered = copy.deepcopy(rows)
        for row in altered[31:]:
            row["binance"]["price"] *= 4
            row["up"]["bid"], row["up"]["ask"] = .10, .11
        original, changed = decisions_for(rows), decisions_for(altered)
        for left, right in zip(original[:31], changed[:31]):
            self.assertEqual(left["features"], right["features"])
            self.assertEqual(left["eligible"], right["eligible"])
            self.assertEqual(research.feature_vector(left, "up", "spot_residual"),
                             research.feature_vector(right, "up", "spot_residual"))

    def test_future_received_source_is_not_a_current_feature(self):
        rows = rows_for_window(last_offset=6000)
        rows[-1]["binance"]["event_ms"] = 5000
        rows[-1]["binance"]["received_ms"] = 6001
        decision = decisions_for(rows)[-1]
        self.assertIsNone(decision["features"]["spot_return_5s_bps"])
        self.assertIsNone(research.feature_vector(decision, "up", "spot_residual"))

    def test_quote_lookup_uses_received_time_not_event_time(self):
        decisions = decisions_for(rows_for_window(last_offset=4000))
        decisions[1]["raw"]["up"]["event_ms"] = 1000
        decisions[1]["raw"]["up"]["received_ms"] = 999
        index = research.next_quote(decisions, [d["sample_ms"] for d in decisions],
                                    1000, "up", decisions[0]["slug"])
        self.assertEqual(index, 2)
        future = copy.deepcopy(decisions[0])
        future["raw"]["up"]["received_ms"] = future["sample_ms"] + 1
        self.assertIsNone(research.feature_vector(future, "up", "book_residual"))

    def test_fit_excludes_labels_not_yet_available(self):
        examples = [{"slug": str(i // 4), "available_ms": 1000,
                     "x": [.5, 1., .5, .1, 0.], "y": .01}
                    for i in range(120)]
        expected = research.fit_ridge(examples, "book_residual", 1000)
        late = {"slug": "future", "available_ms": 1001,
                "x": [.9, 1., .5, .1, 0.], "y": 1e9}
        actual = research.fit_ridge(examples + [late], "book_residual", 1000)
        self.assertIsNotNone(expected)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["training_markets"], 30)
        self.assertFalse(actual["target_is_official_resolution"])

    def test_training_targets_purge_whole_markets_at_boundary(self):
        rows = rows_for_window(0, 299_000) + rows_for_window(300_000, 299_000)
        dataset = data.build_quality_dataset(rows, required_features=())
        decisions = dataset["decisions"]
        examples = research.training_examples(dataset, 600_000, "book_residual")
        self.assertTrue(examples)
        self.assertEqual({x["slug"] for x in examples}, {"btc-updown-5m-0"})
        self.assertTrue(all(x["available_ms"] <= 600_000 for x in examples))
        # Evaluation also excludes a whole market starting inside its embargo.
        result = research.replay(decisions, fixed_model(), .01, begin_ms=360_000)
        self.assertEqual(result["summary"]["attempts"], 0)

    def test_training_target_cannot_cross_a_bad_row_between_gold_segments(self):
        rows = rows_for_window(last_offset=90_000)
        rows[40]["poly_valid"] = False
        dataset = data.build_quality_dataset(rows, data.QualityPolicy(gold_min_contiguous_seconds=30),
                                             required_features=())
        examples = research.training_examples(dataset, 400_000, "book_residual")
        available = {example["available_ms"] for example in examples}
        self.assertIn(30_000, available)
        self.assertTrue(any(ms >= 71_000 for ms in available))
        self.assertFalse(any(41_000 <= ms <= 70_000 for ms in available))


class CashReplayTests(unittest.TestCase):
    def test_insufficient_cash_cannot_buy_the_minimum_order(self):
        result = research.replay(decisions_for(rows_for_window()), fixed_model(), .01, initial_cash=1.)
        self.assertEqual(result["summary"]["proxy_entries"], 0)
        self.assertEqual(result["summary"]["available_cash_end"], 1.)
        self.assertEqual(result["attempts"][0]["status"], "insufficient_cash_for_minimum")
        self.assertEqual(result["cash_ledger"], [])

    def test_observed_fee_change_is_charged_separately_at_entry_and_exit(self):
        rows = rows_for_window()
        rows[1]["microstructure"]["rules"]["fees"]["rate"] = .10
        rows[32]["microstructure"]["rules"]["fees"]["rate"] = .20
        result = research.replay(decisions_for(rows), fixed_model(), .01)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["status"], "closed_proxy_cycle")
        self.assertAlmostEqual(attempt["entry_fee"], 5 * .10 * .5 * .5)
        continuous_exit = 5 * .20 * .59 * .41
        self.assertAlmostEqual(attempt["exit_fee"], max(continuous_exit, round(continuous_exit, 5)))
        self.assertAlmostEqual(result["summary"]["available_cash_end"], 10 + attempt["pnl_usd"])
        self.assertEqual(result["summary"]["actual_fills"], 0)
        self.assertTrue(all(a["quote_proxy"] for a in result["attempts"]))

    def test_future_fee_metadata_is_unknown(self):
        row = raw_row(0, 1000)
        row["microstructure"]["rules"]["metadata_received_ms"] = 1001
        self.assertIsNone(research.fee(row, .50, 5))

    def test_non_executable_exit_market_keeps_cash_locked(self):
        rows = rows_for_window()
        rows[32]["microstructure"]["rules"]["accepting_orders"] = False
        result = research.replay(decisions_for(rows), fixed_model(), .01)
        self.assertEqual(result["summary"]["closed_proxy_cycles"], 0)
        self.assertEqual(result["summary"]["unresolved_positions"], 1)
        self.assertGreater(result["summary"]["locked_cost_usd"], 0)

    def test_unsupported_exit_fee_model_cannot_value_a_round_trip(self):
        rows = rows_for_window()
        rows[32]["microstructure"]["rules"]["fees"]["model"] = "unsupported_curve"
        result = research.replay(decisions_for(rows), fixed_model(), .01)
        self.assertEqual(result["summary"]["closed_proxy_cycles"], 0)
        self.assertEqual(result["summary"]["unresolved_positions"], 1)
        self.assertGreater(result["summary"]["locked_cost_usd"], 0)

    def test_cash_cannot_be_reused_before_the_prior_release_time(self):
        rows = rows_for_window(0, 299_000) + rows_for_window(300_000, 299_000)
        result = research.replay(decisions_for(rows), fixed_model(), .01, cash_delay_ms=400_000)
        entered = [a for a in result["attempts"] if "entry_ms" in a]
        self.assertEqual(len(entered), 2)
        self.assertGreaterEqual(entered[1]["decision_ms"], entered[0]["cash_available_ms"])
        self.assertGreaterEqual(entered[1]["entry_ms"], entered[0]["cash_available_ms"])
        ledger = result["cash_ledger"]
        self.assertEqual([e["ms"] for e in ledger], sorted(e["ms"] for e in ledger))
        cash = 10.
        for event in ledger:
            cash += event["cash_delta"]
            self.assertGreaterEqual(cash, -1e-9)
            self.assertAlmostEqual(cash, event["available_cash"])
        self.assertAlmostEqual(cash, result["summary"]["available_cash_end"])

    def test_missing_exit_keeps_cost_locked_and_blocks_the_next_market(self):
        rows = rows_for_window(0, 30_000) + rows_for_window(300_000, 45_000)
        result = research.replay(decisions_for(rows), fixed_model(), .01)
        attempt = result["attempts"][0]
        self.assertEqual(result["summary"]["proxy_entries"], 1)
        self.assertEqual(result["summary"]["closed_proxy_cycles"], 0)
        self.assertEqual(result["summary"]["unresolved_positions"], 1)
        self.assertAlmostEqual(result["summary"]["locked_cost_usd"], attempt["cost_usd"])
        self.assertAlmostEqual(result["summary"]["available_cash_end"], 10 - attempt["cost_usd"])
        self.assertAlmostEqual(result["summary"]["net_lower_bound_usd"], -attempt["cost_usd"])
        self.assertEqual([e["kind"] for e in result["cash_ledger"]], ["proxy_entry"])

    def test_cash_due_after_observation_end_is_not_available(self):
        decisions = decisions_for(rows_for_window(last_offset=32_000))
        result = research.replay(decisions, fixed_model(), .01, cash_delay_ms=10_000)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["status"], "exit_observed_cash_pending")
        self.assertGreater(attempt["cash_available_ms"], decisions[-1]["sample_ms"])
        self.assertEqual(result["summary"]["closed_proxy_cycles"], 0)
        self.assertAlmostEqual(result["summary"]["available_cash_end"], 10 - attempt["cost_usd"])
        self.assertTrue(all(e["ms"] <= decisions[-1]["sample_ms"] for e in result["cash_ledger"]))

    def test_quote_depth_and_changed_minimum_block_proxy_entry(self):
        for change, expected in (("depth", "entry_capacity"), ("minimum", "entry_minimum_changed")):
            with self.subTest(change=change):
                decisions = decisions_for(rows_for_window())
                if change == "depth":
                    decisions[1]["raw"]["up"]["ask_size"] = 4
                else:
                    decisions[1]["min_order_size"] = 6
                result = research.replay(decisions, fixed_model(), .01)
                self.assertEqual(result["attempts"][0]["status"], expected)
                self.assertEqual(result["summary"]["proxy_entries"], 0)

    def test_ambiguous_minimum_units_do_not_become_share_orders(self):
        decisions = decisions_for(rows_for_window())
        for decision in decisions:
            decision["min_order_size_unit"] = None
        result = research.replay(decisions, fixed_model(), .01)
        self.assertEqual(result["summary"]["proxy_entries"], 0)
        self.assertGreater(result["summary"]["decision_blocks"].get("minimum_order_unit_unknown", 0), 0)

    def test_known_minimum_notional_and_quantity_step_apply_together(self):
        rows = rows_for_window()
        for row in rows:
            row["microstructure"]["rules"].update(min_order_notional=5.2, order_size_step=3.)
        decisions = decisions_for(rows)
        self.assertEqual(research.minimum_shares(decisions[0], .5), 12.)
        result = research.replay(decisions, fixed_model(), .01)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["shares"], 12.)
        self.assertGreaterEqual(attempt["entry_price"] * attempt["shares"], 5.2)
        self.assertEqual(attempt["shares"] % 3, 0)

    def test_changed_quantity_step_cannot_accept_an_invalid_pending_order(self):
        decisions = decisions_for(rows_for_window())
        decisions[1]["rules"]["order_size_step"] = 3.
        result = research.replay(decisions, fixed_model(), .01)
        self.assertEqual(result["summary"]["proxy_entries"], 0)
        self.assertEqual(result["cash_ledger"], [])

    def test_changed_tick_cannot_accept_a_now_invalid_limit(self):
        decisions = decisions_for(rows_for_window())
        decisions[1]["raw"]["microstructure"]["rules"]["tick_size"] = .02
        result = research.replay(decisions, fixed_model(), .01)
        self.assertEqual(result["attempts"][0]["status"], "entry_tick_changed_or_invalid_limit")
        self.assertEqual(result["summary"]["proxy_entries"], 0)

    def test_negative_or_nonfinite_execution_parameters_are_rejected(self):
        decisions = decisions_for(rows_for_window())
        for parameter in ("latency_ms", "cash_delay_ms", "extra_exit_ticks"):
            for value in (-1, float("nan"), float("inf")):
                with self.subTest(parameter=parameter, value=value):
                    with self.assertRaises(ValueError):
                        research.replay(decisions, fixed_model(), .01, **{parameter: value})

    def test_complete_set_quote_condition_is_never_an_actual_fill(self):
        rows = [raw_row(0, 1000, bid=.47, ask=.48)]
        result = research.complete_set_diagnostic(decisions_for(rows), 10.)
        self.assertEqual(len(result["necessary_quote_conditions"]), 1)
        self.assertEqual(result["actual_fills"], 0)
        self.assertFalse(result["necessary_quote_conditions"][0]["executable_profit_established"])
        self.assertFalse(result["necessary_quote_conditions"][0]["two_leg_execution_observed"])


class ResearchPipelineTests(unittest.TestCase):
    def test_cli_reads_verified_archives_and_writes_integrity_bound_no_trade_model(self):
        # 200 synthetic observations exercise real I/O and orchestration, not model research.
        rows = [raw_row(market * 300_000, offset)
                for market in range(100) for offset in (0, 1000)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "results"
            source.mkdir()
            archive = source / "snapshots-synthetic.jsonl.gz"
            with gzip.open(archive, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            archive.with_name(archive.name + ".sha256").write_text(archive_hash + "  " + archive.name)
            stdout = io.StringIO()
            with patch.object(research, "QualityPolicy", return_value=data.QualityPolicy(gold_min_contiguous_seconds=2)), \
                 patch.object(sys, "argv", ["quality_first_research", "--input", str(source), "--output", str(output)]), \
                 contextlib.redirect_stdout(stdout):
                research.main()
            result = json.loads((output / "research.json").read_text())
            frozen = json.loads((output / "frozen_model.json").read_text())
            self.assertEqual(result["source_provenance"]["files"][0]["sha256"], archive_hash)
            self.assertEqual(result["source_hash"], research.digest(result["source_provenance"]))
            self.assertEqual(result["independent_market_windows"], 100)
            self.assertEqual(result["selected_family"], "no_trade")
            self.assertIsNone(frozen["model"])
            frozen_hash = frozen.pop("sha256")
            self.assertEqual(frozen_hash, research.digest(frozen))
            self.assertEqual(frozen_hash, result["frozen_model_sha256"])
            self.assertEqual(frozen["source_hash"], result["source_hash"])
            self.assertGreater(frozen["forward_min_window_start_ms"], frozen["consumed_through_ms"])
            self.assertGreater(frozen["forward_min_window_start_ms"], frozen["frozen_at_ms"])
            self.assertEqual(result["actual_fills"], 0)
            self.assertFalse(result["ten_fill_canary_achieved"])
            self.assertNotIn("source_hash", stdout.getvalue())
            self.assertIn("RESULTS_REQUIRE_SEALING=true", stdout.getvalue())
            tampered = dict(frozen, threshold=999, sha256=frozen_hash)
            (output / "frozen_model.json").write_text(json.dumps(tampered))
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                research.run(source, root / "forward", frozen_model=output / "frozen_model.json")

    def test_holdout_outcome_does_not_choose_or_refit_the_model(self):
        rows = [raw_row(market * 300_000, offset)
                for market in range(100) for offset in (0, 1000)]
        provenance = {"files": [{"path": "synthetic", "sha256": "0" * 64}]}
        outputs = []
        for holdout_outcome in (-1000., 1000.):
            replay_calls = []
            def fit(examples, family, cutoff):
                return {"family": family, "coef": [.2] + [0.] * len(research.FEATURES[family]),
                        "fit_cutoff_ms": cutoff}
            def replay(decisions, model, threshold, initial_cash, **kwargs):
                validation = kwargs.get("end_ms") is not None
                replay_calls.append((model["family"], threshold, validation))
                score = ((1 if model["family"] == "book_residual" else 3)
                         + (1 if threshold == research.THRESHOLDS[-1] else 0))
                return {"summary": {"closed_proxy_cycles": 10, "unresolved_positions": 0,
                                    "net_without_best_trade_usd": score if validation else holdout_outcome}}
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(research, "load_verified_snapshots", return_value=(rows, provenance)), \
                 patch.object(research, "fit_ridge", side_effect=fit) as fitted, \
                 patch.object(research, "replay", side_effect=replay):
                output = Path(directory)
                result = research.run(output / "input", output / "output")
                frozen = json.loads((output / "output" / "frozen_model.json").read_text())
                self.assertEqual(fitted.call_count, len(research.FEATURES))
                self.assertTrue(all(call.args[2] == result["split"]["train_end_ms"]
                                    for call in fitted.call_args_list))
                self.assertEqual([validation for _, _, validation in replay_calls], [True] * 8 + [False] * 2)
                self.assertEqual(result["holdout"]["summary"]["net_without_best_trade_usd"], holdout_outcome)
                outputs.append((frozen["model"], frozen["threshold"], result["selected_family"]))
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
