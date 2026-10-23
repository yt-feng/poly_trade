import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis import offline_paired_cost_audit as audit


class CountingModel:
    def __init__(self):
        self.fit_calls = 0
        self.predict_calls = 0

    def fit(self, features, labels):
        self.fit_calls += 1
        return self

    def predict_proba(self, features):
        self.predict_calls += 1
        up = np.array([0.8, 0.9])[:len(features)]
        return np.column_stack([1 - up, up])


def config_fixture():
    return {"protocol_id": "test", "source_commit": "a" * 40, "features": ["btc_move_1m"],
        "decision_cutoff_seconds": 120, "entry_delay_seconds": 4, "maximum_snapshot_age_seconds": 90,
        "model_params": {"random_state": 1}, "confidence_floor": .8, "favorite_price_floor": .5,
        "depth_thresholds": [3, 5, 7], "slippages": [.005, .01], "latencies_seconds": [2],
        "depth_haircuts": [.1], "minimum_training_runs": 1, "evaluation_block_runs": 12,
        "terminal_proxy_max_gap_seconds": 1, "cash_release_delay_after_close_seconds": 900,
        "evaluation_mode": "paired_cost_gate", "control_reference_results_sha256": "0" * 64,
        "control_reference_candidate_counts": {"0.005": 2, "0.01": 2}}


def current_fixture():
    starts = pd.to_datetime(["2026-01-02T00:00:00Z", "2026-01-02T00:05:00Z"])
    return pd.DataFrame({"btc_move_1m": [1., 2.], "buy_up_price_2m": [.76, .76],
        "buy_down_price_2m": [.26, .26], "buy_up_size_2m": [1000., 1000.],
        "buy_down_size_2m": [1000., 1000.], "spread_up_median_first2m": [.02, .02],
        "spread_down_median_first2m": [.02, .02], "overround_median_first2m": [.01, .01],
        "first_quote_ts": starts, "decision_ts": starts + pd.Timedelta(seconds=120),
        "label_observed_ts": starts + pd.Timedelta(seconds=300), "outcome_up": [0, 1],
        "slug": ["btc-updown-5m-1767312000", "btc-updown-5m-1767312300"],
        "market_id": ["new::one", "new::two"], "run_name": ["new", "new"]}, index=[100, 101])


def reference_row():
    return {"scope": "pooled_single_ledger", "block_index": 0, "slippage": .01,
        "latency_seconds": 2., "depth_haircut": .1, "runs": ["new"], "fills": 1,
        "ending_cash": 10.1234567894, "first_ten_fills": 1, "first_ten_ending_cash": 10.1234567894,
        "first_ten_passed": False, "failure_reason": "sequence_or_net_growth_gate_not_met",
        "locked_shares": 0., "ending_cash_available_ts": None,
        "chain": [{"market_id": "new::one", "cash_before_entry": 10., "status": "quote_exit", "payout": 6.}],
        "attempts": [{"market_id": "new::two", "status": "delayed_entry_depth_insufficient"}]}


class OfflinePairedCostAuditTests(unittest.TestCase):
    def test_one_model_prediction_feeds_both_arms_and_slippages(self):
        config = config_fixture()
        train = pd.DataFrame({"btc_move_1m": [0., 1.], "outcome_up": [0, 1]})
        model = CountingModel()
        scored = audit.score_once(model, train, current_fixture(), config["features"])
        with audit.base.isolated_signal_contract(config["features"], 124):
            arms = audit.candidate_arms(scored, config)
        self.assertEqual(model.fit_calls, 1)
        self.assertEqual(model.predict_calls, 1)
        self.assertEqual(arms["legacy_control"][.01]["market_id"].tolist(), ["new::one", "new::two"])
        self.assertEqual(arms["cost_checked"][.01]["market_id"].tolist(), ["new::two"])
        self.assertEqual(arms["cost_checked"][.005]["market_id"].tolist(), ["new::one", "new::two"])
        for slippage in config["slippages"]:
            control = arms["legacy_control"][slippage].set_index("market_id")
            treatment = arms["cost_checked"][slippage].set_index("market_id")
            for market, row in treatment.iterrows():
                self.assertEqual(row["q_up"], control.loc[market, "q_up"])
                self.assertAlmostEqual(control.loc[market, "edge"] - row["edge"], slippage)

    def test_frozen_scores_require_exact_row_alignment(self):
        frozen = audit.FrozenScores(pd.Series([.2, .8], index=[7, 9]))
        with self.assertRaisesRegex(ValueError, "alignment"):
            frozen.predict_proba(pd.DataFrame({"x": [1, 2]}, index=[9, 7]))

    def test_reference_fingerprint_ignores_added_provenance_and_float_noise(self):
        old = reference_row()
        new = copy.deepcopy(old)
        new["ending_cash"] += 1e-11
        new["chain"][0].update(side="up", q_side=.9, decision_ts="new evidence", exit_quote_evidence={"size": 1000})
        self.assertEqual(audit.results_fingerprint([old]), audit.results_fingerprint([new]))
        new["chain"][0]["payout"] += .01
        self.assertNotEqual(audit.results_fingerprint([old]), audit.results_fingerprint([new]))

    def test_reference_requires_both_result_and_candidate_count_match(self):
        rows = [reference_row()]
        config = config_fixture()
        config["control_reference_results_sha256"] = audit.results_fingerprint(rows)
        counts = config["control_reference_candidate_counts"]
        self.assertTrue(audit.compare_control(rows, counts, config)["control_reproduced"])
        changed = dict(counts); changed["0.01"] = 1
        self.assertFalse(audit.compare_control(rows, changed, config)["control_reproduced"])
        config["control_reference_results_sha256"] = "0" * 64
        self.assertFalse(audit.compare_control(rows, counts, config)["control_reproduced"])

    def test_evaluate_preserves_full_scores_members_and_marks_control_mismatch(self):
        config = config_fixture()
        train = pd.DataFrame({"btc_move_1m": np.arange(100), "outcome_up": [0, 1] * 50,
            "market_id": [f"old::{i}" for i in range(100)], "slug": [f"old-{i}" for i in range(100)],
            "run_name": "old", "label_observed_ts": pd.Timestamp("2026-01-01T00:00:00Z")})
        features = pd.concat([train, current_fixture()], ignore_index=True)
        model = CountingModel()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(audit.base, "prepare_archive", return_value=(features, ["old", "new"], {}, {})), \
             patch.object(audit, "ExtraTreesClassifier", return_value=model), \
             patch.object(audit.subprocess, "check_output", return_value="a" * 40 + "\n"), \
             patch.object(audit.base, "replay_policy", return_value={"fills": 0, "ending_cash": 10., "chain": [], "attempts": []}):
            output = Path(temp) / "private"
            result = audit.evaluate(Path(temp), config, output)
            self.assertEqual(model.fit_calls, 1)
            self.assertEqual(model.predict_calls, 1)
            self.assertTrue(result["strategy_evaluated"])
            self.assertEqual(result["evaluation_status"], "evaluated_control_mismatch")
            self.assertFalse(result["causal_comparison_valid"])
            self.assertEqual(len(pd.read_csv(output / "scores.csv")), 2)
            self.assertEqual(len(pd.read_csv(output / "training-members.csv")), 100)
            self.assertEqual(set(result["arms"]), {"legacy_control", "cost_checked"})
            self.assertEqual(result["actual_model_predict_calls"], 1)
            self.assertEqual(result["fit_records"][0]["training_unique_market_count"], 100)
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((output / "summary.json").read_text())["control_reproduced"], False)


if __name__ == "__main__":
    unittest.main()
