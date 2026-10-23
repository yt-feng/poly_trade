import unittest

import pandas as pd

from analysis import offline_policy_audit as audit


class OfflinePolicyAuditTests(unittest.TestCase):
    def _entry_fixture(self, outcome=float("nan")):
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        row = {"market_id": "a::market", "run_name": "a", "slug": "market", "side": "up",
            "entry_ts": start + pd.Timedelta(seconds=124), "market_close_ts": start + pd.Timedelta(seconds=300),
            "slippage": 0.005, "fill_price": 0.505, "sell_up_size_2m": 1000,
            "sell_down_size_2m": 1000, "outcome_up": outcome,
            "label_observed_ts": start + pd.Timedelta(seconds=300) if pd.notna(outcome) else pd.NaT}
        quotes = pd.DataFrame({"ts_utc": [start + pd.Timedelta(seconds=x) for x in (124, 128, 240, 299)],
            "buy_up_cents": [50, 60, 60, 60], "buy_up_size": [1000] * 4,
            "sell_up_cents": [20] * 4, "sell_up_size": [1000] * 4})
        return pd.DataFrame([row]), {"a::market": quotes}

    def test_no_future_exit_keeps_debit_and_unresolved_position(self):
        candidates, quotes = self._entry_fixture()
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=0,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(result["fills"], 1)
        self.assertLess(result["ending_cash"], 0.00001)
        self.assertGreater(result["locked_shares"], 0)
        self.assertEqual(result["chain"][0]["status"], "unresolved_position")

    def test_no_exit_terminal_loss_remains_on_ledger(self):
        candidates, quotes = self._entry_fixture(outcome=0)
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=0,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["chain"][0]["payout"], 0)
        self.assertFalse(result["first_ten_passed"])
        self.assertTrue(result["ending_cash_available_ts"].endswith("00:20:00+00:00"))

    def test_latency_uses_delayed_entry_price_and_depth(self):
        candidates, quotes = self._entry_fixture()
        result = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertAlmostEqual(result["chain"][0]["entry_fill_price"], .605)
        quotes["a::market"].loc[1, "buy_up_size"] = 1
        missing = audit.replay_policy(candidates, ["a"], quotes, latency_seconds=4,
            depth_haircut=1, thresholds=(3, 5, 7))
        self.assertEqual(missing["fills"], 0)
        self.assertEqual(missing["ending_cash"], 10)

    def test_availability_rejects_later_and_unknown_observations(self):
        self.assertEqual(audit.feature_available_seconds("btc_move_2m"), 120)
        self.assertEqual(audit.feature_available_seconds("btc_move_4m"), 240)
        with self.assertRaises(ValueError):
            audit.feature_available_seconds("unregistered_future_statistic")

    def test_training_uses_only_labels_known_before_decision_and_other_markets(self):
        rows = pd.DataFrame({"run_name": ["a"] * 4, "slug": ["old", "late", "same", "equal"],
            "label_observed_ts": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:03:00Z",
                                                "2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z"])})
        current = pd.DataFrame({"slug": ["same"], "decision_ts": pd.to_datetime(["2026-01-01T00:02:00Z"])})
        self.assertEqual(audit.causal_training_rows(rows, ["a"], current)["slug"].tolist(), ["old"])

    def test_blocks_are_disjoint_include_final_partial_block(self):
        runs = [str(x) for x in range(29)]
        blocks = audit.disjoint_blocks(runs, 12)
        self.assertEqual([len(b) for b in blocks], [12, 12, 5])
        self.assertEqual([r for b in blocks for r in b], runs)
        with self.assertRaises(ValueError):
            audit.disjoint_blocks(["same", "same"], 1)

    def test_signal_contract_restored_after_exception(self):
        original = audit.signal.FEATURES
        entry = audit.signal.ENTRY_SECONDS_AFTER_START
        with self.assertRaises(RuntimeError):
            with audit.isolated_signal_contract(["btc_move_1m"], 130):
                raise RuntimeError("test")
        self.assertIs(audit.signal.FEATURES, original)
        self.assertEqual(audit.signal.ENTRY_SECONDS_AFTER_START, entry)

    def test_post_decision_changes_do_not_change_quality_or_allowed_features(self):
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        rows = []
        for second in (0, 60, 120, 180, 240, 299):
            row = {key: 100.0 for key in audit.source.REQUIRED_NUMERIC}
            row.update(ts_iso=(start + pd.Timedelta(seconds=second)).isoformat(), slug="btc-updown-5m-1767225600",
                run_name="a", source_file="fixture", target_price=1000.0, final_price=1000 + second / 60,
                buy_up_cents=60, sell_up_cents=58, buy_down_cents=42, sell_down_cents=40,
                mid_up_cents=59, mid_down_cents=41, spread_up_cents=2, spread_down_cents=2)
            rows.append(row)
        original = pd.DataFrame(rows)
        changed = original.copy()
        changed.loc[4, "final_price"] = 1100.0
        changed.loc[4, "buy_up_cents"] = float("nan")
        qa, qb = (audit.source.prepare_quotes(frame) for frame in (original, changed))
        self.assertTrue(audit.prefix_quality(qa, 90).iloc[0]["prefix_quality_passed"])
        self.assertTrue(audit.prefix_quality(qb, 90).iloc[0]["prefix_quality_passed"])
        fa, fb = (audit.source.build_features(q, strict_quality=False) for q in (qa, qb))
        allowed = [name for name in audit.source.LOGIT_FEATURES if audit.feature_available_seconds(name) <= 120]
        pd.testing.assert_frame_equal(fa[allowed], fb[allowed])
        self.assertNotEqual(fa.iloc[0]["btc_move_4m"], fb.iloc[0]["btc_move_4m"])
        truncated = audit.prefix_quality(qa[qa["ts_utc"] <= start + pd.Timedelta(seconds=120)], 90)
        self.assertTrue(truncated.iloc[0]["prefix_quality_passed"])
        self.assertTrue(pd.isna(truncated.iloc[0]["qualified_outcome_up"]))
        near = audit.prefix_quality(qa, 90, terminal_gap_seconds=1)
        self.assertEqual(near.iloc[0]["terminal_proxy_gap_seconds"], -1)
        self.assertEqual(near.iloc[0]["label_observed_ts"], start + pd.Timedelta(seconds=300))
        strict = audit.prefix_quality(qa, 90, terminal_gap_seconds=0)
        self.assertTrue(pd.isna(strict.iloc[0]["qualified_outcome_up"]))


if __name__ == "__main__":
    unittest.main()
