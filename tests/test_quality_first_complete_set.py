"""Synthetic delayed two-leg diagnostics. No order submission or market access."""
from pathlib import Path
import copy
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import quality_first_research as research
from test_quality_first_research import raw_row, decisions_for


def quote_rows(last_ms=6000):
    return [raw_row(0, ms, bid=.47, ask=.48) for ms in range(0, last_ms + 1, 1000)]


def diagnose(rows):
    return research.complete_set_diagnostic(decisions_for(rows), 10.)


def delay(result, milliseconds):
    return next(row for row in result["necessary_quote_conditions"][0]["delayed_checks"]
                if row["latency_ms"] == milliseconds)


class CompleteSetDiagnosticTests(unittest.TestCase):
    def test_new_prearrival_receipts_and_price_failure_are_separate_facts(self):
        rows = quote_rows()
        for side in ("up", "down"):
            rows[1][side].update(bid=.50, ask=.51, received_ms=200, event_ms=200)
        result = diagnose(rows)
        check = delay(result, 1000)
        self.assertEqual(check["receipt_after_decision"], {"up": True, "down": True})
        self.assertEqual(check["receipt_vs_arrival_bounds"]["minimum_ms"], -800)
        self.assertEqual(check["receipt_vs_arrival_bounds"]["maximum_ms"], -800)
        self.assertEqual(check["oldest_quote_age_at_observed_ms"], 800)
        self.assertTrue(check["quote_price_cost_above_payout"])
        self.assertTrue(check["quote_total_cost_above_payout"])
        facts = check["causal_classification"]["observed_facts"]
        self.assertIn("both_quote_receipts_advanced_after_decision", facts)
        self.assertIn("both_refreshed_quotes_predate_hypothetical_arrival", facts)
        self.assertIn("observed_prices_alone_exceed_payout", facts)
        self.assertFalse(check["causal_classification"]["quote_lifetime_identified"])
        self.assertFalse(check["quotes_support_complete_set"])
        summary = result["root_cause_summary"]
        self.assertEqual(summary["new_independent_observations"], 0)
        self.assertEqual(summary["by_latency_ms"]["1000"]["both_receipts_before_arrival"], 1)
        self.assertEqual(summary["by_latency_ms"]["1000"]["observed_prices_alone_above_payout"], 1)

    def test_cost_attribution_does_not_override_receipt_acceptance_gate(self):
        rows = quote_rows()
        for side in ("up", "down"):
            rows[1][side].update(received_ms=500, event_ms=500)
        result = diagnose(rows)
        check = delay(result, 1000)
        self.assertFalse(check["quote_total_cost_not_below_payout"])
        self.assertFalse(check["quotes_support_complete_set"])
        self.assertTrue(all("quote_received_before_arrival" in leg["reasons"] for leg in check["legs"].values()))
        self.assertFalse(result["root_cause_summary"]["selection_or_latency_gate_changed"])

    def test_fees_and_prices_have_distinct_attribution(self):
        rows = quote_rows()
        rows[1]["microstructure"]["rules"]["fees"]["rate"] = .20
        check = delay(diagnose(rows), 1000)
        self.assertAlmostEqual(check["quote_price_cost_usd"], 4.8)
        self.assertFalse(check["quote_price_cost_above_payout"])
        self.assertTrue(check["quote_total_cost_above_payout"])
        self.assertIn("observed_fees_eliminate_price_only_discount", check["causal_classification"]["observed_facts"])

    def test_unknown_future_remains_unknown_in_attribution(self):
        result = diagnose(quote_rows(last_ms=0))
        for check in result["necessary_quote_conditions"][0]["delayed_checks"]:
            self.assertEqual(check["receipt_after_decision"], {"up": None, "down": None})
            self.assertIsNone(check["receipt_vs_arrival_bounds"]["minimum_ms"])
            self.assertFalse(check["receipt_vs_arrival_bounds"]["both_legs_observed"])
            self.assertIsNone(check["oldest_quote_age_at_observed_ms"])
            self.assertIsNone(check["quote_price_cost_above_payout"])
            self.assertIsNone(check["quote_total_cost_not_below_payout"])
        self.assertEqual(result["root_cause_summary"]["by_latency_ms"]["1000"]["unknown_cost_checks"], 1)

    def test_saved_report_attribution_adds_no_sample_and_does_not_mutate_report(self):
        generated = diagnose(quote_rows())
        saved = copy.deepcopy(generated)
        saved.pop("root_cause_summary")
        candidate = saved["necessary_quote_conditions"][0]
        for check in candidate["delayed_checks"]:
            for field in research.complete_set_check_attribution(candidate, check):
                check.pop(field)
        before = copy.deepcopy(saved)
        summary = research.complete_set_root_cause_summary(saved)
        self.assertEqual(saved, before)
        self.assertEqual(summary, generated["root_cause_summary"])
        self.assertEqual(summary["existing_candidate_count"], 1)
        self.assertEqual(summary["new_independent_observations"], 0)
        self.assertFalse(summary["exact_quote_lifetime_identified"])

    def test_surviving_received_quotes_and_two_sided_tick_stress_are_separate(self):
        result = diagnose(quote_rows())
        self.assertEqual(len(result["necessary_quote_conditions"]), 1)
        candidate = result["necessary_quote_conditions"][0]
        self.assertEqual(candidate["sample_ms"], 0)
        self.assertEqual(candidate["shares_per_leg"], 5)
        for latency in (1000, 3000):
            check = delay(result, latency)
            self.assertTrue(check["quotes_support_complete_set"])
            self.assertEqual(check["causal_classification"]["arrival_execution_state"],
                             "unobserved_no_order_or_exchange_acknowledgement")
            self.assertEqual(check["observed_ms"], latency)
            self.assertTrue(all(leg["received_ms"] >= check["arrival_ms"] for leg in check["legs"].values()))
            self.assertFalse(check["extra_tick_stress"]["quotes_support_complete_set"])
            expected_stress = 5 * .49 * 2 + 2 * research.fee(quote_rows()[0], .49, 5)
            self.assertAlmostEqual(check["extra_tick_stress"]["quote_cost_usd"], expected_stress)
            self.assertIn("stress_cost_not_below_redemption", check["extra_tick_stress"]["reasons"])

    def test_disappeared_opportunity_remains_and_does_not_select_a_better_later_row(self):
        rows = quote_rows()
        for side in ("up", "down"):
            rows[1][side].update(bid=.48, ask=.49)
        result = diagnose(rows)
        self.assertEqual(len(result["necessary_quote_conditions"]), 1)
        early = delay(result, 1000)
        self.assertEqual(early["observed_ms"], 1000)
        self.assertFalse(early["quotes_support_complete_set"])
        self.assertIn("cost_not_below_redemption", early["reasons"])
        self.assertTrue(delay(result, 3000)["quotes_support_complete_set"])

    def test_backdated_event_does_not_make_a_prearrival_received_quote_available(self):
        rows = quote_rows()
        rows[1]["down"].update(received_ms=999, event_ms=1000)
        result = diagnose(rows)
        check = delay(result, 1000)
        self.assertEqual(check["observed_ms"], 1000)
        self.assertIn("quote_received_before_arrival", check["legs"]["down"]["reasons"])
        self.assertTrue(check["one_leg_only_quote_support"])
        self.assertFalse(check["quotes_support_complete_set"])
        self.assertTrue(delay(result, 3000)["quotes_support_complete_set"])

    def test_missing_future_observations_keep_every_latency_record(self):
        result = diagnose(quote_rows(last_ms=0))
        checks = result["necessary_quote_conditions"][0]["delayed_checks"]
        self.assertEqual([row["latency_ms"] for row in checks], [1000, 3000])
        for check in checks:
            self.assertFalse(check["quotes_support_complete_set"])
            self.assertIn("arrival_snapshot_unobserved", check["reasons"])
        self.assertEqual(result["actual_fills"], 0)

    def test_single_leg_depth_failure_does_not_become_an_atomic_fill(self):
        rows = quote_rows()
        rows[1]["down"]["ask_size"] = 4
        check = delay(diagnose(rows), 1000)
        self.assertIn("insufficient_ask_depth", check["legs"]["down"]["reasons"])
        self.assertTrue(check["one_leg_only_quote_support"])
        self.assertTrue(check["second_leg_failure_possible"])
        self.assertFalse(check["two_leg_execution_observed"])
        self.assertFalse(check["cash_receipt_observed"])
        self.assertEqual(check["actual_fills"], 0)
        self.assertFalse(check["quotes_support_complete_set"])

    def test_same_slug_with_changed_condition_or_tokens_is_not_the_same_pair(self):
        for changed in ("condition", "tokens", "book_token"):
            with self.subTest(changed=changed):
                rows = quote_rows()
                if changed == "condition":
                    rows[1]["microstructure"]["rules"]["condition_id"] = "other-condition"
                elif changed == "tokens":
                    rows[1]["up_token_id"] = "other-up-token"
                else:
                    rows[1]["up"]["token_id"] = "other-up-token"
                check = delay(diagnose(rows), 1000)
                self.assertFalse(check["quotes_support_complete_set"])
                self.assertTrue(any("identity" in reason for leg in check["legs"].values()
                                    for reason in leg["reasons"]))

    def test_delayed_fee_parameters_can_eliminate_the_original_discount(self):
        rows = quote_rows()
        rows[1]["microstructure"]["rules"]["fees"]["rate"] = .20
        result = diagnose(rows)
        check = delay(result, 1000)
        expected = 5 * .48 * 2 + 2 * (5 * .20 * .48 * .52)
        self.assertAlmostEqual(check["quote_cost_usd"], expected)
        self.assertFalse(check["quotes_support_complete_set"])
        self.assertTrue(delay(result, 3000)["quotes_support_complete_set"])

    def test_current_minimum_and_step_cannot_resize_the_original_intent(self):
        for changed in ("minimum", "step"):
            with self.subTest(changed=changed):
                rows = quote_rows()
                if changed == "minimum":
                    rows[1]["microstructure"]["rules"]["min_order_size"] = 6
                    for side in ("up", "down"):
                        rows[1]["microstructure"]["poly_" + side]["min_order_size"] = 6
                else:
                    rows[1]["microstructure"]["rules"]["order_size_step"] = 2.
                result = diagnose(rows)
                self.assertEqual(result["necessary_quote_conditions"][0]["shares_per_leg"], 5)
                check = delay(result, 1000)
                self.assertFalse(check["quotes_support_complete_set"])
                self.assertIn("current_minimum_unmet", check["legs"]["up"]["reasons"])

    def test_changed_tick_must_still_accept_original_decision_limits(self):
        rows = quote_rows()
        rows[1]["microstructure"]["rules"]["tick_size"] = .02
        for side in ("up", "down"):
            rows[1]["microstructure"]["poly_" + side]["tick_size"] = .02
        check = delay(diagnose(rows), 1000)
        self.assertIn("decision_limit_off_current_tick_grid", check["legs"]["up"]["reasons"])
        self.assertFalse(check["quotes_support_complete_set"])

    def test_delayed_quote_cannot_move_the_original_limit(self):
        rows = quote_rows()
        rows[1]["up"].update(bid=.49, ask=.50)
        check = delay(diagnose(rows), 1000)
        self.assertAlmostEqual(check["legs"]["up"]["decision_limit"], .49)
        self.assertIn("decision_limit_exceeded", check["legs"]["up"]["reasons"])
        self.assertFalse(check["quotes_support_complete_set"])

    def test_scope_excluded_quotes_do_not_create_candidates(self):
        decisions = decisions_for(quote_rows())
        for decision in decisions:
            decision["evaluation_scope_included"] = False
        result = research.complete_set_diagnostic(decisions, 10.)
        self.assertEqual(result["necessary_quote_conditions"], [])
        decisions[0]["evaluation_scope_included"] = True
        result = research.complete_set_diagnostic(decisions, 10.)
        self.assertEqual(len(result["necessary_quote_conditions"]), 1)
        self.assertIn("outside_predefined_sampling_scope", delay(result, 1000)["reasons"])

    def test_large_gap_is_not_positive_latency_evidence(self):
        rows = [quote_rows()[0], raw_row(0, 3000, bid=.47, ask=.48)]
        check = delay(diagnose(rows), 3000)
        self.assertIn("arrival_path_gap_or_market_change", check["reasons"])
        self.assertFalse(check["quotes_support_complete_set"])

    def test_future_changes_cannot_rewrite_the_first_candidate(self):
        original = diagnose(quote_rows())["necessary_quote_conditions"][0]
        rows = quote_rows()
        for row in rows[1:]:
            row["up"].update(bid=.80, ask=.81)
        changed = diagnose(rows)["necessary_quote_conditions"][0]
        for field in ("sample_ms", "shares_per_leg", "quote_cost_usd", "original_quote_legs"):
            self.assertEqual(changed[field], original[field])
        self.assertFalse(changed["executable_profit_established"])
        self.assertFalse(changed["two_leg_execution_observed"])


if __name__ == "__main__":
    unittest.main()
