"""Synthetic source contracts; no parameters or observations from an episode."""
import copy
import json
import unittest

from analysis.settlement_source_coverage import audit_records, validate_policy


START = 1_800_000_000_000
END = START + 300_000
CONDITION = "0x" + "a" * 64


def policy(**changes):
    result = {"decision_seconds_before_end": 40, "anchor_max_age_ms": 800,
              "reference_max_age_ms": 2400, "metadata_max_age_ms": 90_000,
              "snapshot_scope_start_ms": START, "snapshot_scope_end_ms": END - 1}
    result.update(changes)
    return result


def snapshot(ms=None):
    ms = END - 40_000 - 100 if ms is None else ms
    slug = f"btc-updown-5m-{START // 1000}"
    twap = {"price": "100.000000000000000001", "received_ms": ms - 10,
            "event_ms": ms - 100, "publisher_ms": ms - 50, "connection_id": "session-a",
            "source": "crypto_prices_twap_sixty", "window_seconds": 60,
            "payload": {"symbol": "btc/usd", "timestamp": ms - 100,
                        "window_s": 60, "full_accuracy_value": "100000000000000000001", "value": 100}}
    rule = {"slug": slug, "condition_id": CONDITION, "metadata_received_ms": ms - 500,
            "published_price_to_beat": "100.000000000000000001",
            "price_to_beat_received_ms": START + 2000,
            "price_to_beat_path": "polymarket.client_api.crypto-price.openPrice",
            "reference_kind": "chainlink_twap_60", "twap_lookback_seconds": 60.0,
            "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams"}
    return {"schema_version": 2, "asset": "btc", "slug": slug, "window_start_ms": START,
            "sample_ms": ms, "up_token_id": "1001", "down_token_id": "1002",
            "binance_source": "binance_spot_ws", "binance_symbol": "BTCUSDT", "binance_valid": True,
            "binance": {"price": "100.1", "received_ms": ms - 100, "event_ms": ms - 200},
            "microstructure": {"schema_version": 3, "asof_ms": ms, "rules": rule,
                "twap60": twap, "matching_resolution_reference": copy.deepcopy(twap),
                "spot_returns_and_vol": {"60s": {"warmed": True, "valid_intervals": 60,
                                                 "realized_vol_bps": 2.0, "return_bps": -0.5}},
                "valid": {key: True for key in ("twap60", "matching_reference_feed", "official_threshold", "resolution_reference")}}}


def label(outcome="Up", when=END + 5000):
    return {"schema_version": 3, "kind": "official_clob_winner", "slug": snapshot()["slug"],
            "condition_id": CONDITION, "winning_outcome": outcome, "available_at_ms": when,
            "source": "clob/markets/condition_id", "actual_settlement_time_ms": None,
            "final_reference_price": None}


def one(row=None, labels=(), config=None):
    return audit_records([snapshot() if row is None else row], labels, policy() if config is None else config)["per_market"][0]


class PolicyTests(unittest.TestCase):
    def test_explicit_policy_and_no_defaults(self):
        self.assertEqual(validate_policy(policy()), policy())
        for invalid in ({}, {**policy(), "unknown": 1}, {**policy(), "anchor_max_age_ms": True},
                        {**policy(), "decision_seconds_before_end": float("nan")},
                        {**policy(), "decision_seconds_before_end": 300},
                        {**policy(), "decision_seconds_before_end": 0.0001},
                        {**policy(), "metadata_max_age_ms": -1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_policy(invalid)

    def test_scope_requires_both_bounds(self):
        p = policy()
        del p["snapshot_scope_end_ms"]
        with self.assertRaises(ValueError):
            validate_policy(p)


class SelectionTests(unittest.TestCase):
    def test_valid_source_is_only_field_coverage(self):
        result = audit_records([snapshot()], [label()], policy())
        row = result["per_market"][0]
        self.assertTrue(row["candidate_field_coverage"])
        self.assertEqual(row["source_evidence"], "legacy_published_snapshot_only")
        self.assertTrue(row["accumulated_same_oracle_integral_unverified"])
        self.assertFalse(result["limitations"]["strategy_ready"])
        self.assertEqual(row["label"]["cash_available"], "unverified")
        self.assertEqual(result["summary"]["n_joint_with_label"], 1)
        json.dumps(result, allow_nan=False)

    def test_select_time_before_quality_and_never_future_row(self):
        early = snapshot(END - 40_000 - 500)
        selected = snapshot()
        selected["microstructure"]["twap60"] = None
        future = snapshot(END - 40_000 + 1)
        result = audit_records([future, early, selected], [], policy())["per_market"][0]
        self.assertEqual(result["selected_sample_ms"], selected["sample_ms"])
        self.assertFalse(result["candidate_field_coverage"])
        self.assertIn("twap_price_missing_or_invalid", result["failure_reasons"])

    def test_missing_anchor_and_partial_windows_remain_in_denominator(self):
        p = policy(snapshot_scope_start_ms=START + 1, snapshot_scope_end_ms=END + 300_001)
        result = audit_records([snapshot()], [], p)
        self.assertEqual(result["summary"]["n_expected_market_decisions"], 3)
        self.assertEqual(result["summary"]["n_with_anchor"], 1)
        self.assertEqual(result["summary"]["n_partial_source_windows"], 2)
        self.assertIn("anchor_missing", result["per_market"][1]["failure_reasons"])

    def test_observed_scope_does_not_extrapolate_to_future_anchor(self):
        p = {k: v for k, v in policy().items() if not k.startswith("snapshot_scope")}
        result = one(config=p)
        self.assertIn("anchor_outside_sample_scope", result["failure_reasons"])
        self.assertFalse(result["candidate_field_coverage"])

    def test_duplicate_source_metadata_does_not_create_conflict(self):
        a, b = snapshot(), snapshot()
        a["__source__"], b["__source__"] = {"filename": "a", "row_index": 1}, {"filename": "b", "row_index": 9}
        result = audit_records([a, b], [], policy())["per_market"][0]
        self.assertTrue(result["candidate_field_coverage"])
        self.assertEqual(result["anchor_evidence"]["source_record"], a["__source__"])

    def test_conflicting_selected_duplicate_fails(self):
        a, b = snapshot(), snapshot()
        b["microstructure"]["rules"]["published_price_to_beat"] = "99"
        result = audit_records([a, b], [], policy())["per_market"][0]
        self.assertIn("anchor_duplicate_conflict", result["failure_reasons"])
        self.assertFalse(result["candidate_field_coverage"])

    def test_bad_identity_on_latest_row_is_not_silently_removed(self):
        a, b = snapshot(END - 40_000 - 500), snapshot()
        b["slug"] = "bad"
        result = audit_records([a, b], [], policy())["per_market"][0]
        self.assertEqual(result["selected_sample_ms"], b["sample_ms"])
        self.assertIn("snapshot_identity_mismatch", result["failure_reasons"])


class SourceTests(unittest.TestCase):
    def test_derived_rule_does_not_override_missing_or_different_source(self):
        for source in (None, "https://data.chain.link/streams/eth-usd-twap-60s-streams",
                       "https://data.chain.link/streams/btc-usd-twap-30s-streams",
                       "https://data.chain.link.example/streams/btc-usd-twap-60s-streams"):
            row = snapshot()
            row["microstructure"]["rules"]["resolution_source"] = source
            self.assertIn("resolution_source_missing_or_mismatched", one(row)["failure_reasons"])
            self.assertFalse(one(row)["candidate_field_coverage"])

    def test_fresh_at_snapshot_but_expired_at_decision(self):
        for group in ("twap60", "binance", "rules"):
            row = snapshot(END - 40_000 - 800)
            if group == "rules":
                row["microstructure"][group]["metadata_received_ms"] = row["sample_ms"] - 89_900
                stage = "identity"
            else:
                target = row[group] if group == "binance" else row["microstructure"][group]
                target["received_ms"] = row["sample_ms"] - 2300
                if group == "twap60":
                    row["microstructure"]["matching_resolution_reference"] = copy.deepcopy(target)
                stage = "vol60" if group == "binance" else "twap"
            with self.subTest(group=group):
                self.assertFalse(one(row)["feature_availability"][stage])

    def test_nested_ptb_is_sufficient_and_has_no_freshness_ttl(self):
        row = snapshot()
        row["microstructure"]["rules"]["price_to_beat_received_ms"] = START
        self.assertTrue(one(row)["candidate_field_coverage"])

    def test_future_clocks_fail_even_if_flags_true(self):
        for group, clock in (("twap60", "event_ms"), ("twap60", "received_ms"), ("binance", "event_ms")):
            row = snapshot()
            target = row[group] if group == "binance" else row["microstructure"][group]
            target[clock] = row["sample_ms"] + 1
            with self.subTest(group=group, clock=clock):
                self.assertFalse(one(row)["candidate_field_coverage"])
        row = snapshot()
        row["microstructure"]["rules"]["price_to_beat_received_ms"] = row["sample_ms"] + 1
        self.assertIn("ptb_receipt_missing_or_noncausal", one(row)["failure_reasons"])

    def test_receipt_does_not_refresh_stale_event(self):
        row = snapshot()
        row["microstructure"]["twap60"]["event_ms"] = row["sample_ms"] - 9000
        self.assertFalse(one(row)["feature_availability"]["twap"])

    def test_precision_source_and_matching_reference(self):
        row = snapshot()
        row["microstructure"]["twap60"]["price"] = "100.000000000000000002"
        self.assertIn("twap_published_value_mismatch", one(row)["failure_reasons"])
        row = snapshot()
        row["official_price_to_beat"] = "100.000000000000000002"
        self.assertIn("ptb_duplicate_conflict", one(row)["failure_reasons"])
        row = snapshot()
        row["microstructure"]["matching_resolution_reference"]["connection_id"] = "other"
        self.assertIn("matching_reference_mismatch", one(row)["failure_reasons"])

    def test_value_fallback_not_divided_and_payload_event_matches(self):
        row = snapshot()
        twap = row["microstructure"]["twap60"]
        del twap["payload"]["full_accuracy_value"]
        twap["price"] = "100"
        row["microstructure"]["matching_resolution_reference"] = copy.deepcopy(twap)
        self.assertTrue(one(row)["feature_availability"]["twap"])
        twap["payload"]["timestamp"] += 1
        self.assertIn("twap_payload_event_clock_mismatch", one(row)["failure_reasons"])

    def test_source_window_and_boolean_type_rejections(self):
        for field, value in (("source", "crypto_prices_chainlink"), ("window_seconds", True)):
            row = snapshot()
            row["microstructure"]["twap60"][field] = value
            self.assertFalse(one(row)["feature_availability"]["twap"])
        row = snapshot()
        row["microstructure"]["twap60"]["payload"]["window_s"] = 30
        self.assertFalse(one(row)["candidate_field_coverage"])
        row = snapshot()
        row["microstructure"]["rules"]["metadata_received_ms"] = True
        self.assertFalse(one(row)["feature_availability"]["identity"])

    def test_polybolt_shape_is_not_silently_admitted_to_legacy_audit(self):
        row = snapshot()
        twap = row["microstructure"]["twap60"]
        twap["source"] = "polybolt_crypto_twap_60"
        twap["topic"] = "prices.crypto.twap"
        twap["payload"] = {
            "symbol": "btcusd",
            "timestamp": twap["event_ms"],
            "value": twap["price"],
            "windowSeconds": 60,
        }
        self.assertFalse(one(row)["feature_availability"]["twap"])
        self.assertIn("twap_legacy_identity_mismatch", one(row)["failure_reasons"])

    def test_warmed_and_realized_vol_units(self):
        row = snapshot()
        vol = row["microstructure"]["spot_returns_and_vol"]["60s"]
        vol.update(realized_vol_bps=0.0, return_bps=0.0, valid_intervals=59)
        self.assertTrue(one(row)["feature_availability"]["vol60"])
        self.assertTrue(one(row)["zero_volatility"])
        vol["valid_intervals"] = 58
        self.assertFalse(one(row)["feature_availability"]["vol60"])
        vol.update(valid_intervals=60, warmed=False, realized_vol_bps=None)
        self.assertFalse(one(row)["feature_availability"]["vol60"])


class LabelTests(unittest.TestCase):
    def test_absent_anchor_identity_is_unknown_not_conflicted(self):
        for condition in (None, "malformed"):
            row = snapshot()
            row["microstructure"]["rules"]["condition_id"] = condition
            result = one(row, labels=[label()])["label"]
            self.assertEqual(result["status"], "identity_unverified")
            self.assertEqual(result["failure_reasons"], ["label_identity_unverified"])
            self.assertFalse(result["official_label_observed"])
        result = audit_records([], [label()], policy())["per_market"][0]["label"]
        self.assertEqual(result["status"], "identity_unverified")

    def test_labels_do_not_change_feature_selection_or_quality(self):
        without = one(labels=[])
        with_label = one(labels=[label()])
        for field in ("selected_sample_ms", "feature_availability", "failure_reasons", "candidate_field_coverage"):
            self.assertEqual(without[field], with_label[field])
        self.assertEqual(with_label["label"]["first_observed_ms"], END + 5000)

    def test_first_observed_not_end_and_conflicts_fail(self):
        self.assertEqual(one(labels=[label(when=END+9000), label(), label()])["label"]["first_observed_ms"], END+5000)
        self.assertIn("label_outcome_conflict", one(labels=[label(), label("Down")])["label"]["failure_reasons"])

    def test_wrong_condition_and_pre_end_label_rejected(self):
        wrong = label()
        wrong["condition_id"] = "0x" + "b" * 64
        self.assertIn("label_identity_conflict", one(labels=[wrong])["label"]["failure_reasons"])
        self.assertIn("label_availability_invalid", one(labels=[label(when=END-1)])["label"]["failure_reasons"])

    def test_horizon_and_nonofficial_variants_are_separate(self):
        self.assertEqual(one(labels=[])["label"]["status"], "label_horizon_not_covered")
        p = policy(label_observed_through_ms=END+6000)
        self.assertEqual(one(labels=[], config=p)["label"]["status"], "label_not_observed_in_selected_files")
        self.assertFalse(one(labels=[label(when=END+7000)], config=p)["label"]["official_label_observed"])
        nonofficial = {**label(), "kind": "quote_markout_not_fill_pnl"}
        ws = {"kind": "market_resolved", "available_at_ms": END+1000, "payload": {"market": CONDITION}}
        result = audit_records([snapshot()], [nonofficial, ws], policy())
        self.assertEqual(result["summary"]["unparsed_ws_resolution_events"], 1)
        self.assertEqual(result["summary"]["n_official_label"], 0)


if __name__ == "__main__":
    unittest.main()
