import json
import unittest
from pathlib import Path

from analysis.canary_evidence_ledger import evaluate, load_records


FIXTURES = Path(__file__).parent / "fixtures"


def private_record(index=1, *, date="2026-10-01", window="private-window-001"):
    return {
        "evidence_id": f"private-{index:03d}",
        "provenance": "private_execution_receipt",
        "window_id": window,
        "entry_utc": f"{date}T00:00:00Z",
        "entry_status": "CONFIRMED",
        "entry_trade_id": f"entry-{index:03d}",
        "entry_transaction_ref": f"entry-tx-{index:03d}",
        "exit_status": "CONFIRMED",
        "exit_trade_id": f"exit-{index:03d}",
        "exit_transaction_ref": f"exit-tx-{index:03d}",
        "exit_reconciled": True,
        "order_acknowledged": True,
        "cancel_reconciled": True,
        "fee_reconciled": True,
        "settlement_reconciled": True,
        "account_reconciled": True,
        "gross_pnl_usd": "0.10",
        "fees_usd": "0.01",
        "lower_bound_pnl_usd": "0.09",
        "stress_3s_lower_bound_usd": "0.05",
        "stress_extra_exit_tick_lower_bound_usd": "0.04",
    }


class CanaryEvidenceLedgerTests(unittest.TestCase):
    def test_zero_fixture_reports_explicit_missing_counts(self):
        report = evaluate(load_records(FIXTURES / "evidence_zero.json"))
        self.assertEqual(report["observed"]["real_confirmed_fill_count"], 0)
        self.assertEqual(report["observed"]["qualifying_private_receipt_records"], 0)
        self.assertEqual(report["observed"]["public_quote_records"], 1)
        self.assertEqual(report["observed"]["paper_simulation_records"], 1)
        self.assertGreater(report["missing_counts"]["execution_evidence"], 0)
        self.assertGreater(report["missing_counts"]["independent_windows"], 0)
        self.assertGreater(report["missing_counts"]["independent_utc_dates"], 0)
        self.assertIn("public_or_synthetic_evidence_only", report["blockers"])
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["promotion_allowed"])

    def test_fully_reconciled_synthetic_fixture_never_counts_as_real(self):
        report = evaluate(load_records(FIXTURES / "evidence_synthetic_reconciled.json"))
        self.assertEqual(report["observed"]["synthetic_receipt_records"], 10)
        self.assertEqual(report["observed"]["real_confirmed_fill_count"], 0)
        self.assertEqual(report["observed"]["complete_roundtrips"], 0)
        self.assertEqual(report["observed"]["qualifying_private_receipt_records"], 0)
        self.assertGreater(report["missing_counts"]["ten_canary_roundtrips"], 0)
        self.assertFalse(report["promotion_allowed"])
        self.assertIn("excluded_synthetic_receipt", report["excluded_record_reasons"])

    def test_private_receipt_is_counted_but_one_record_cannot_pass_gates(self):
        report = evaluate([private_record()])
        self.assertEqual(report["phase"], "pre_canary_research")
        self.assertEqual(report["observed"]["real_confirmed_fill_count"], 1)
        self.assertEqual(report["observed"]["complete_roundtrips"], 1)
        self.assertEqual(report["observed"]["cost_adjusted_pnl_lower_bound_usd"], "0.09")
        self.assertEqual(report["observed"]["exit_reconciliation_rate"], "1")
        self.assertGreater(report["missing_counts"]["execution_evidence"], 0)
        self.assertNotIn("ten_canary_roundtrips", report["blockers"])
        self.assertIn("ten_canary_roundtrips", report["post_canary_completion"]["blockers"])
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["promotion_allowed"])

    def test_ten_roundtrips_are_post_canary_completion_gate(self):
        record = private_record()
        pre = evaluate([record], phase="pre_canary_research")
        post = evaluate([record], phase="post_canary_completion")
        self.assertNotIn("ten_canary_roundtrips", pre["blockers"])
        self.assertIn("ten_canary_roundtrips", pre["post_canary_completion"]["blockers"])
        self.assertIn("ten_canary_roundtrips", post["blockers"])
        self.assertFalse(post["post_canary_completion"]["complete"])

    def test_unknown_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate([], phase="before_or_after_canary")

    def test_missing_stress_fields_are_a_blocker_even_with_private_receipt(self):
        record = private_record()
        del record["stress_3s_lower_bound_usd"]
        del record["stress_extra_exit_tick_lower_bound_usd"]
        report = evaluate([record])
        self.assertEqual(report["observed"]["three_second_stress_missing_records"], 1)
        self.assertEqual(report["observed"]["extra_exit_tick_stress_missing_records"], 1)
        self.assertEqual(report["missing_counts"]["three_second_stress_lower_bound_positive"], 1)
        self.assertEqual(report["missing_counts"]["extra_exit_tick_stress_lower_bound_positive"], 1)


if __name__ == "__main__":
    unittest.main()
