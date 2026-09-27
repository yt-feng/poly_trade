"""Pure synthetic evidence classification; no account, wallet, SDK or network."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from execution_reason_codes import classify, summarize


def fixture(**updates):
    out = dict(signal_qualified=False, order_submitted=True, order_terminal=True,
               order_status="CANCELED", requested_shares="5",
               confirmed_filled_shares="0", fill_reconciliation_complete=False)
    out.update(updates)
    return out


class TerminalEvidence(unittest.TestCase):
    def test_missing_quantity_is_unknown(self):
        r = fixture(); del r["confirmed_filled_shares"]
        self.assertEqual(classify(r)["reason_code"], "ORDER_TERMINAL_FILL_QUANTITY_UNKNOWN")
        self.assertFalse(classify(r)["final_for_episode"])

    def test_none_quantity_is_unknown(self):
        self.assertFalse(classify(fixture(confirmed_filled_shares=None))["final_for_episode"])

    def test_empty_quantity_is_unknown(self):
        self.assertFalse(classify(fixture(confirmed_filled_shares=""))["final_for_episode"])

    def test_observed_zero_without_coverage_is_pending(self):
        self.assertEqual(classify(fixture())["reason_code"], "ORDER_TERMINAL_FILL_RECONCILIATION_PENDING")

    def test_explicit_zero_complete_is_unfilled_not_cash(self):
        r=classify(fixture(fill_reconciliation_complete=True))
        self.assertTrue(r["final_for_episode"])
        self.assertEqual(r["reason_code"], "ORDER_TERMINAL_UNFILLED")
        self.assertFalse(r["live_action_taken"])
        self.assertFalse(r["private_fill_inferred"])

    def test_missing_quantity_overrides_complete_assertion(self):
        self.assertFalse(classify(fixture(confirmed_filled_shares=None, fill_reconciliation_complete=True))["final_for_episode"])

    def test_matched_order_overrides_complete_assertion(self):
        self.assertFalse(classify(fixture(order_status="MATCHED", fill_reconciliation_complete=True))["final_for_episode"])

    def test_mined_trade_overrides_cancelled_order(self):
        self.assertFalse(classify(fixture(trade_status="MINED", fill_reconciliation_complete=True))["final_for_episode"])

    def test_retrying_trade_overrides_complete_assertion(self):
        self.assertFalse(classify(fixture(trade_status="RETRYING", fill_reconciliation_complete=True))["final_for_episode"])

    def test_delayed_order_is_pending(self):
        self.assertFalse(classify(fixture(order_status="DELAYED"))["final_for_episode"])

    def test_confirmed_status_and_zero_quantity_conflict(self):
        self.assertFalse(classify(fixture(trade_status="CONFIRMED", fill_reconciliation_complete=True))["final_for_episode"])

    def test_matched_shares_override_zero_confirmed(self):
        self.assertFalse(classify(fixture(matched_shares="2", fill_reconciliation_complete=True))["final_for_episode"])

    def test_negative_matched_shares_rejected(self):
        self.assertRaises(ValueError, classify, fixture(matched_shares="-1"))

    def test_string_coverage_is_not_true(self):
        self.assertFalse(classify(fixture(fill_reconciliation_complete="true"))["final_for_episode"])

    def test_integer_coverage_is_not_true(self):
        self.assertFalse(classify(fixture(fill_reconciliation_complete=1))["final_for_episode"])

    def test_submission_reference_preserves_unknown_over_no_signal(self):
        r=fixture(order_submitted=False, submission_reference="synthetic-reference", confirmed_filled_shares=None)
        self.assertEqual(classify(r)["reason_code"], "ORDER_TERMINAL_FILL_QUANTITY_UNKNOWN")

    def test_entry_terminal_alias_is_not_ignored(self):
        self.assertFalse(classify(fixture(order_terminal=False, entry_order_terminal=True))["final_for_episode"])

    def test_string_false_terminal_is_not_true(self):
        r=dict(signal_qualified=True, planned_shares="5", min_order_size="5", order_terminal="false", confirmed_filled_shares="0")
        self.assertEqual(classify(r)["reason_code"], "FILL_STATUS_UNKNOWN")
        self.assertFalse(classify(r)["final_for_episode"])

    def test_missing_quantity_fallback_is_not_zero(self):
        r=dict(signal_qualified=True, planned_shares="5", min_order_size="5", order_terminal=True)
        self.assertFalse(classify(r)["final_for_episode"])

    def test_late_partial_remains_open(self):
        r=classify(fixture(confirmed_filled_shares="2"))
        self.assertEqual(r["reason_code"], "PARTIAL_CONFIRMED_FILL")
        self.assertFalse(r["final_for_episode"])

    def test_late_partial_then_expired_is_not_unfilled(self):
        r=classify(fixture(confirmed_filled_shares="2", market_expired=True))
        self.assertEqual(r["reason_code"], "EXPIRED_AWAITING_OFFICIAL_RESOLUTION")

    def test_reconciliation_unknown_overrides_everything(self):
        self.assertEqual(classify(fixture(reconciliation_unknown=True, fill_reconciliation_complete=True))["reason_code"], "RECONCILIATION_UNKNOWN")

    def test_rejected_after_submission_missing_quantity_not_final(self):
        r=classify(fixture(order_terminal=False, order_status="ERROR", confirmed_filled_shares=None))
        self.assertEqual(r["reason_code"], "ORDER_REJECTED")
        self.assertFalse(r["final_for_episode"])

    def test_rejected_with_covered_zero_keeps_cause(self):
        r=classify(fixture(order_terminal=False, order_status="REJECTED", fill_reconciliation_complete=True))
        self.assertEqual(r["reason_code"], "ORDER_REJECTED")
        self.assertTrue(r["final_for_episode"])

    def test_tradeids_alone_are_not_a_zero_fill_receipt(self):
        self.assertFalse(classify(fixture(trade_ids=["synthetic-t1"]))["final_for_episode"])

    def test_no_input_mutation(self):
        r=fixture(trade_status="MINED"); before=copy.deepcopy(r)
        classify(r); self.assertEqual(r, before)

    def test_sequence_never_disappears_between_match_and_partial(self):
        sequence=[fixture(confirmed_filled_shares=None), fixture(order_status="MATCHED"), fixture(trade_status="MINED"), fixture(confirmed_filled_shares="2")]
        self.assertEqual(summarize(sequence)["final_episodes"], 0)

    def test_preflight_no_signal_unchanged(self):
        self.assertEqual(classify(dict(signal_qualified=False))["reason_code"], "NO_QUALIFIED_OPPORTUNITY")

    def test_pending_status_trimmed(self):
        self.assertFalse(classify(fixture(trade_status=" mined ", fill_reconciliation_complete=True))["final_for_episode"])

    def test_pending_statuses_cartesian_contract(self):
        for key in ("order_status", "trade_status"):
            for status in ("MATCHED", "MINED", "RETRYING", "PENDING", "DELAYED", "CONFIRMED"):
                for complete in (False, True):
                    with self.subTest(key=key,status=status,complete=complete):
                        self.assertFalse(classify(fixture(**{key:status,"fill_reconciliation_complete":complete}))["final_for_episode"])

if __name__ == "__main__":
    unittest.main()
