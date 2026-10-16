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


def synthetic_provenance(rows):
    return {"files": [{"path": "synthetic", "sha256": "0" * 64, "rows": len(rows),
                       "first_sample_ms": min(row["sample_ms"] for row in rows),
                       "last_sample_ms": max(row["sample_ms"] for row in rows)}]}


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

    def test_exit_quantity_must_match_changed_step_even_above_new_minimum(self):
        rows = rows_for_window()
        for row in rows:
            row["microstructure"]["rules"]["min_order_notional"] = 4.5
        rows[32]["microstructure"]["rules"]["order_size_step"] = 2.
        decisions = decisions_for(rows)
        self.assertEqual(research.minimum_shares(decisions[32], .59), 8.)
        result = research.replay(decisions, fixed_model(), .01)
        attempt = result["attempts"][0]
        self.assertEqual(attempt["shares"], 9.)
        self.assertEqual(attempt["status"], "exit_size_step_changed")
        self.assertEqual(result["summary"]["closed_proxy_cycles"], 0)
        self.assertAlmostEqual(result["summary"]["locked_cost_usd"], attempt["cost_usd"])

    def test_stress_exit_price_must_meet_known_minimum_notional(self):
        rows = rows_for_window()
        for row in rows:
            row["microstructure"]["rules"]["min_order_notional"] = 2.5
        for side in ("up", "down"):
            rows[32][side].update(bid=.51, ask=.52)
        decisions = decisions_for(rows)
        baseline = research.replay(decisions, fixed_model(), .01)
        stressed = research.replay(decisions, fixed_model(), .01, extra_exit_ticks=2)
        self.assertEqual(baseline["summary"]["closed_proxy_cycles"], 1)
        self.assertEqual(stressed["attempts"][0]["shares"], 5.)
        self.assertEqual(stressed["attempts"][0]["status"], "exit_minimum_changed")
        self.assertEqual(stressed["summary"]["closed_proxy_cycles"], 0)
        self.assertGreater(stressed["summary"]["locked_cost_usd"], 0)

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
    def test_protocol_contract_binds_policy_features_execution_and_source_files(self):
        contract = research.protocol_contract()
        self.assertEqual(contract["quality_policy"]["gold_min_contiguous_seconds"],
                         data.QualityPolicy().gold_min_contiguous_seconds)
        self.assertEqual(contract["features"], research.FEATURES)
        self.assertEqual(contract["hold_ms"], research.HOLD_MS)
        self.assertEqual(contract["maximum_positions"], 1)
        self.assertEqual(contract["fill_model"], "taker_quote_proxy")
        self.assertEqual(set(contract["implementation_sha256"]),
                         {"quality_first_data.py", "quality_first_research.py"})
        for name, expected in contract["implementation_sha256"].items():
            self.assertEqual(expected, hashlib.sha256((ROOT / "analysis" / name).read_bytes()).hexdigest())

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
            expected_protocol = research.protocol_contract()
            expected_protocol["quality_policy"]["gold_min_contiguous_seconds"] = 2
            self.assertEqual(frozen["protocol_sha256"], research.digest(expected_protocol))
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

    def test_forward_refuses_policy_implementation_or_initial_cash_changes(self):
        rows = [raw_row(market * 300_000, offset)
                for market in range(100) for offset in (0, 1000)]
        provenance = synthetic_provenance(rows)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, provenance)):
            root = Path(directory)
            research.run(root / "input", root / "discovery")
            model_path = root / "discovery" / "frozen_model.json"
            frozen = json.loads(model_path.read_text())
            baseline_contract = research.protocol_contract()
            self.assertEqual(frozen["protocol_sha256"], research.digest(baseline_contract))
            forward = research.run(root / "input", root / "forward", frozen_model=model_path)
            self.assertEqual(forward["forward_episode_id"], frozen["sha256"])
            self.assertFalse(forward["cumulative_across_runs"])
            with self.assertRaisesRegex(ValueError, "initial cash changed"):
                research.run(root / "input", root / "wrong_cash", initial_cash=11., frozen_model=model_path)
            for change in ("policy", "features", "execution", "source"):
                changed = copy.deepcopy(baseline_contract)
                if change == "policy":
                    changed["quality_policy"]["gold_min_contiguous_seconds"] += 1
                elif change == "features":
                    changed["features"]["book_residual"].append("unregistered_feature")
                elif change == "execution":
                    changed["cash_delay_ms"] += 1
                else:
                    changed["implementation_sha256"]["quality_first_data.py"] = "f" * 64
                with self.subTest(change=change), patch.object(research, "protocol_contract", return_value=changed):
                    with self.assertRaisesRegex(ValueError, "protocol or implementation changed"):
                        research.run(root / "input", root / change, frozen_model=model_path)

    def test_invalid_market_identity_stays_in_funnel_without_breaking_run_or_replay(self):
        rows = [raw_row(market * 300_000, offset)
                for market in range(100) for offset in (0, 1000)]
        invalid = raw_row(30_000_000, 0)
        invalid["slug"] = "invalid-market-identity"
        invalid["microstructure"]["rules"]["slug"] = invalid["slug"]
        rows.append(invalid)
        dataset = data.build_quality_dataset(rows, required_features=())
        bad_decision = dataset["decisions"][-1]
        self.assertIsNone(bad_decision["window_start_ms"])
        self.assertFalse(bad_decision["eligible"])
        replay = research.replay([bad_decision], fixed_model(), .01)
        self.assertEqual(replay["summary"]["attempts"], 0)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))):
            root = Path(directory)
            result = research.run(root / "input", root / "output")
            self.assertEqual(result["independent_market_windows"], 100)
            self.assertEqual(result["quality_funnel"]["input_rows"], 201)
            self.assertGreater(result["quality_funnel"]["layer_rejections"]["base_integrity"]
                               .get("market_identity_invalid", 0), 0)

    def test_holdout_outcome_does_not_choose_or_refit_the_model(self):
        rows = [raw_row(market * 300_000, offset)
                for market in range(100) for offset in (0, 1000)]
        provenance = synthetic_provenance(rows)
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


class ResearchChallengerTests(unittest.TestCase):
    def model(self, cutoff=1_000_000):
        return {"family": "spot_residual", "coef": [.20] + [0.] * len(research.FEATURES["spot_residual"]),
                "fit_cutoff_ms": cutoff}

    def resign(self, payload):
        payload = copy.deepcopy(payload)
        payload.pop("sha256", None)
        payload.pop("episode_id", None)
        payload["episode_id"] = research.digest(payload)
        payload["sha256"] = research.digest(payload)
        return payload

    def test_challenger_export_reuses_training_without_changing_no_trade_promotion(self):
        rows = [raw_row(market * 300_000, offset)
                for market in range(100) for offset in (0, 1000)]
        def fitted(examples, family, cutoff):
            return {"family": family, "coef": [0.] * (len(research.FEATURES[family]) + 1),
                    "fit_cutoff_ms": cutoff}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))), \
             patch.object(research, "fit_ridge", side_effect=fitted) as fit:
            root = Path(directory)
            result = research.run(root / "input", root / "output", research_challenger="spot_residual")
            frozen = json.loads((root / "output/frozen_model.json").read_text())
            challenger = json.loads((root / "output/research_challenger.json").read_text())
            self.assertEqual(fit.call_count, 2)
            self.assertTrue(all(call.args[2] == result["split"]["train_end_ms"] for call in fit.call_args_list))
            self.assertEqual(challenger["model"]["fit_cutoff_ms"], result["split"]["train_end_ms"])
            self.assertEqual(result["selected_family"], "no_trade")
            self.assertIsNone(frozen["model"])
            self.assertEqual(challenger["threshold"], .02)
            self.assertFalse(challenger["promotion_eligible"])
            self.assertFalse(result["promotion"]["canary_eligible"])
            self.assertEqual(challenger["forward_max_window_end_ms"] - challenger["forward_min_window_start_ms"], 86_400_000)
            self.assertGreater(challenger["forward_min_window_start_ms"], challenger["frozen_at_ms"])
            self.assertGreater(challenger["forward_min_window_start_ms"], challenger["consumed_through_ms"])
            self.assertFalse(result["research_challenger_export"]["refit_for_export"])
            self.assertFalse(result["research_challenger_export"]["holdout_used_for_export"])
            unsigned = dict(challenger)
            digest = unsigned.pop("sha256")
            self.assertEqual(digest, research.digest(unsigned))
            episode_id = unsigned.pop("episode_id")
            self.assertEqual(episode_id, research.digest(unsigned))

    def test_unavailable_fit_does_not_export_a_fake_challenger(self):
        rows = [raw_row(market * 300_000, 0) for market in range(10)]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))):
            root = Path(directory)
            result = research.run(root / "input", root / "output", research_challenger="spot_residual")
            self.assertEqual(result["research_challenger_export"]["status"], "model_unavailable")
            self.assertFalse((root / "output/research_challenger.json").exists())
            self.assertTrue((root / "output/frozen_model.json").exists())

    def test_unknown_challenger_or_export_during_forward_is_rejected_before_data_access(self):
        with patch.object(research, "load_verified_snapshots") as load:
            with self.assertRaisesRegex(ValueError, "unknown research challenger"):
                research.run(Path("unused"), Path("unused"), research_challenger="new_threshold_winner")
            with self.assertRaisesRegex(ValueError, "cannot accompany frozen"):
                research.run(Path("unused"), Path("unused"), frozen_model=Path("unused"), research_challenger="spot_residual")
            load.assert_not_called()

    def test_finished_episode_replay_filters_old_and_expired_markets_before_any_diagnostic(self):
        challenger = research.research_challenger_payload(self.model(), "source", 2_000_000, 10., 3_000_000)
        begin, end = challenger["forward_min_window_start_ms"], challenger["forward_max_window_end_ms"]
        starts = [market * 300_000 for market in range(5)] + [begin + market * 300_000 for market in range(3)] + [end, end + 300_000]
        rows = sum((rows_for_window(start) for start in starts), [])
        calls = []
        original_replay = research.replay
        def recorded(decisions, *args, **kwargs):
            calls.append((decisions, kwargs))
            return original_replay(decisions, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))), \
             patch.object(research, "fit_ridge") as fit, \
             patch.object(research, "replay", side_effect=recorded), \
             patch.object(research, "complete_set_diagnostic", wraps=research.complete_set_diagnostic) as complete:
            root = Path(directory)
            source = root / "challenger.json"
            source.write_text(json.dumps(challenger))
            result = research.run(root / "input", root / "output", frozen_model=source)
            fit.assert_not_called()
            self.assertEqual(result["sample_role"], "prospective_research_challenger")
            self.assertEqual(result["evaluation_status"], "bounded_episode_replay")
            self.assertTrue(result["episode_window_closed"])
            self.assertEqual(result["forward_episode_id"], challenger["episode_id"])
            self.assertEqual(len(calls), 2)
            for decisions, options in calls:
                self.assertEqual(options["begin_ms"], begin)
                self.assertEqual(options["end_ms"], end)
                self.assertTrue(all(begin <= row["window_start_ms"] and row["window_end_ms"] <= end for row in decisions))
                self.assertEqual(len({row["slug"] for row in decisions}), 3)
            self.assertTrue(all(begin <= row["sample_ms"] < end for row in complete.call_args.args[0]))
            self.assertFalse(result["promotion_eligible"])
            self.assertEqual(result["actual_fills"], 0)
            self.assertFalse(result["ten_fill_canary_achieved"])

    def test_no_new_observations_are_reported_as_awaiting_not_success(self):
        challenger = research.research_challenger_payload(self.model(), "source", 4_000_000, 10., 5_000_000)
        # A frozen forward replay does not need the discovery split's 10 markets.
        rows = [raw_row(0, 0)]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))), \
             patch.object(research, "fit_ridge") as fit:
            root = Path(directory)
            source = root / "challenger.json"
            source.write_text(json.dumps(challenger))
            result = research.run(root / "input", root / "output", frozen_model=source)
            self.assertEqual(result["evaluation_status"], "awaiting_new_data")
            self.assertEqual(result["new_episode_observation_count"], 0)
            self.assertEqual(result["forward"]["summary"]["proxy_entries"], 0)
            self.assertEqual(result["complete_set_diagnostic"]["necessary_quote_conditions"], [])
            fit.assert_not_called()

    def test_no_btc_observations_are_awaiting_and_never_train(self):
        challenger = research.research_challenger_payload(self.model(), "source", 4_000_000, 10., 5_000_000)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=([], synthetic_provenance([raw_row(0, 0)]))), \
             patch.object(research, "fit_ridge") as fit:
            root = Path(directory)
            source = root / "challenger.json"
            source.write_text(json.dumps(challenger))
            result = research.run(root / "input", root / "output", frozen_model=source)
            self.assertEqual(result["evaluation_status"], "awaiting_new_data")
            self.assertIsNone(result["observed_last_ms"])
            self.assertEqual(result["forward"]["summary"]["proxy_entries"], 0)
            self.assertFalse(result["input_reaches_episode_end"])
            fit.assert_not_called()

    def test_record_time_outside_episode_is_excluded_even_with_in_episode_market(self):
        challenger = research.research_challenger_payload(self.model(), "source", 4_000_000, 10., 5_000_000)
        begin, end = challenger["forward_min_window_start_ms"], challenger["forward_max_window_end_ms"]
        rows = [raw_row(begin, -1000), raw_row(begin, 0), raw_row(begin, end - begin)]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))), \
             patch.object(research, "replay", wraps=research.replay) as replay, \
             patch.object(research, "complete_set_diagnostic", wraps=research.complete_set_diagnostic) as complete:
            root = Path(directory)
            source = root / "challenger.json"
            source.write_text(json.dumps(challenger))
            result = research.run(root / "input", root / "output", frozen_model=source)
            self.assertEqual(result["new_episode_observation_count"], 1)
            self.assertEqual(len(replay.call_args_list), 2)
            for call in replay.call_args_list + [complete.call_args]:
                self.assertEqual([row["sample_ms"] for row in call.args[0]], [begin])

    def test_episode_statistics_exclude_outside_rows_and_end_is_half_open(self):
        challenger = research.research_challenger_payload(self.model(), "source", 4_000_000, 10., 5_000_000)
        begin, end = challenger["forward_min_window_start_ms"], challenger["forward_max_window_end_ms"]
        cases = [
            ([raw_row(0, 0)], [], "awaiting_new_data", False),
            ([raw_row(end, 0)], [], "no_observations_within_episode", False),
            ([raw_row(0, 0), raw_row(end, 0)], [], "no_observations_within_episode", False),
            ([raw_row(begin, 0), raw_row(end, 0)], [begin], "bounded_episode_replay", False),
            ([raw_row(0, 0), raw_row(begin, 0), raw_row(begin + 300_000, 0), raw_row(end, 0)],
             [begin, begin + 300_000], "bounded_episode_replay", False),
            ([raw_row(begin, -1), raw_row(begin, 0), raw_row(end - 300_000, 299_999), raw_row(end, 0)],
             [begin, end - 1], "bounded_episode_replay", True),
            ([raw_row(end - 300_000, 299_000)], [end - 1000], "bounded_episode_replay", True),
            ([raw_row(end - 300_000, 298_999), raw_row(end, 0)],
             [end - 1001], "bounded_episode_replay", False),
        ]
        for rows, expected_times, status, reaches_end in cases:
            with self.subTest(times=[r["sample_ms"] for r in rows]), tempfile.TemporaryDirectory() as directory, \
                 patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))), \
                 patch.object(research, "replay", wraps=research.replay) as replay:
                root = Path(directory)
                source = root / "challenger.json"
                source.write_text(json.dumps(challenger))
                result = research.run(root / "input", root / "output", frozen_model=source)
                self.assertEqual(result, json.loads((root / "output/research.json").read_text()))
                self.assertEqual(result["evaluation_status"], status)
                self.assertEqual(result["new_episode_observation_count"], len(expected_times))
                self.assertEqual(result["quality_funnel"]["input_rows"], len(expected_times))
                self.assertEqual(result["quality_funnel"]["input_windows"], len(expected_times))
                self.assertEqual(result["independent_market_windows"], len(expected_times))
                self.assertEqual(result["observed_first_ms"], min(expected_times, default=None))
                self.assertEqual(result["observed_last_ms"], max(expected_times, default=None))
                self.assertEqual(result["input_reaches_episode_end"], reaches_end)
                for call in replay.call_args_list:
                    self.assertEqual([d["sample_ms"] for d in call.args[0]], expected_times)
                full = result["input_summary"]
                self.assertEqual(full["quality_funnel"]["input_rows"], len(rows))
                self.assertEqual(full["independent_market_windows"], len({r["window_start_ms"] for r in rows}))
                self.assertEqual(full["observed_first_ms"], rows[0]["sample_ms"])
                self.assertEqual(full["observed_last_ms"], rows[-1]["sample_ms"])
                scope = result["evaluation_scope"]
                self.assertEqual(scope["included_market_windows"] + scope["excluded_boundary_market_windows"], len(expected_times))

    def test_episode_summary_preserves_causal_warmup_and_layer_counts(self):
        challenger = research.research_challenger_payload(self.model(), "source", 4_000_000, 10., 5_000_000)
        begin, end = challenger["forward_min_window_start_ms"], challenger["forward_max_window_end_ms"]
        rows = [raw_row(begin - 300_000, offset) for offset in range(295_000, 300_000, 1000)]
        rows += [raw_row(begin, offset) for offset in range(0, 100_000, 1000)]
        rows[-1]["poly_valid"] = False
        rows += [raw_row(end, 0)]
        full_dataset = data.build_quality_dataset(rows, required_features=())
        original = copy.deepcopy(full_dataset)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(research, "load_verified_snapshots", return_value=(rows, synthetic_provenance(rows))), \
             patch.object(research, "build_quality_dataset", return_value=full_dataset), \
             patch.object(research, "replay", wraps=research.replay) as replay:
            root = Path(directory)
            source = root / "challenger.json"
            source.write_text(json.dumps(challenger))
            result = research.run(root / "input", root / "output", frozen_model=source)
        selected = replay.call_args.args[0]
        self.assertEqual(len(selected), 100)
        self.assertIsNotNone(selected[0]["features"]["spot_return_5s_bps"])
        self.assertIsNone(selected[0]["features"]["up_mid_change_5s"])
        self.assertEqual(selected[0]["features"], original["decisions"][5]["features"])
        for before, after in zip(original["decisions"], full_dataset["decisions"]):
            self.assertEqual(before["training_gold"], after["training_gold"])
            self.assertEqual(before["features"], after["features"])
        funnel = result["quality_funnel"]
        self.assertEqual(funnel["input_rows"], 100)
        self.assertEqual(funnel["decision_eligible_rows"], 99)
        self.assertEqual(funnel["gold_training_rows"], 99)
        self.assertEqual(funnel["gold_training_segments"], 1)
        self.assertEqual(funnel["layer_accepted_rows"]["execution_quality"], 99)
        self.assertEqual(funnel["layer_rejections"]["execution_quality"],
                         {reason: 1 for reason in original["decisions"][-2]["quality"]["execution_quality"]["reasons"]})
        self.assertEqual(result["input_summary"]["quality_funnel"], original["funnel"])

    def test_invalid_or_unbounded_episode_cannot_be_resigned_into_validity(self):
        original = research.research_challenger_payload(self.model(), "source", 4_000_000, 10., 5_000_000)
        cases = []
        before_freeze = copy.deepcopy(original)
        before_freeze["forward_min_window_start_ms"] = 4_800_000
        cases.append(before_freeze)
        unbounded = copy.deepcopy(original)
        unbounded["forward_max_window_end_ms"] += 300_000
        unbounded["expires_at_ms"] += 300_000
        cases.append(unbounded)
        mismatch = copy.deepcopy(original)
        mismatch["expires_at_ms"] -= 1
        cases.append(mismatch)
        after_fit = copy.deepcopy(original)
        after_fit["model"]["fit_cutoff_ms"] = after_fit["consumed_through_ms"] + 1
        cases.append(after_fit)
        negative_fit = copy.deepcopy(original)
        negative_fit["model"]["fit_cutoff_ms"] = -1
        cases.append(negative_fit)
        unknown = copy.deepcopy(original)
        unknown["model"]["family"] = "book_residual"
        cases.append(unknown)
        threshold = copy.deepcopy(original)
        threshold["threshold"] = .01
        cases.append(threshold)
        for case in cases:
            case = self.resign(case)
            case.pop("sha256")
            with self.subTest(case=case), self.assertRaises(ValueError):
                research.validate_research_challenger(case)


if __name__ == "__main__":
    unittest.main()
