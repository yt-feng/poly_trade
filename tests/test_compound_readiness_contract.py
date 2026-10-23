from __future__ import annotations

import unittest

from tools.compound_readiness_report import build_report


def _paper(*, stable: bool) -> dict:
    block = {
        "split": "holdout_block_1",
        "first_ten_fills": 10,
        "first_ten_passed": stable,
    }
    row = {
        "selection_all_tick_depth_slippage_gate": True,
        "holdout_all_tick_depth_slippage_gate": True,
        "exact_ten_tick_depth_gate": True,
        "holdout_block_stress": [block],
    }
    return {
        "protocol": {
            "same_ledger": True,
            "required_fills": 10,
            "initial_cash_usd": 10.0,
            "minimum_order_notional_usd": 0.0,
            "minimum_order_shares": 5.0,
        },
        "gates": {
            "h30_move_uncertainty": row,
            "h38_move_uncertainty_midpath": row,
        },
    }


def _state(*, stable: bool, binding: bool, receipts: bool = False) -> dict:
    return {
        "paper_causality_verified": True,
        "paper_split_integrity_verified": True,
        "actual_fills": 10 if binding else 0,
        "same_ledger_compounding_rows": 10 if binding else 0,
        "live_orders_enabled": False,
        "receipt_boundary_status": {
            "authenticated_owner_scoped_bundle_available": binding,
            "btc_five_minute_bundle_available": binding,
            "verified_canary_fills": 10 if binding else 0,
            "owner_scoped_settlement_rows": 10 if receipts else 0,
            "owner_scoped_cash_release_rows": 10 if receipts else 0,
            "owner_scoped_balance_checkpoint_rows": 10 if receipts else 0,
            "settlement_evidence_verified": receipts,
            "cash_release_evidence_verified": receipts,
            "balance_checkpoint_verified": receipts,
        },
        "latest_orderfilled_binding_audit": {
            "verification": {
                "current_owner_bundle_available": binding,
                "synthetic_only": not binding,
            },
            "decision": {
                "same_ledger_compounding_rows": 10 if binding else 0,
                "canary_ready": binding,
            },
            "evidence_boundary": "synthetic_contract_fixture",
        },
    }


class CompoundReadinessContractTest(unittest.TestCase):
    def test_research_preparation_does_not_require_completed_trades(self):
        report = build_report({
            "paper_causality_verified": True,
            "paper_split_integrity_verified": True,
        }, _paper(stable=True))
        lifecycle = report["lifecycle"]
        self.assertTrue(lifecycle["offline_research_allowed"])
        self.assertTrue(lifecycle["paper_candidate_ready_for_execution_preparation"])
        self.assertEqual(lifecycle["preparation_blockers"], [])
        self.assertFalse(lifecycle["completed_canary_evidence_verified"])
        self.assertFalse(lifecycle["canary_launch_authorized"])

    def test_completed_evidence_is_not_execution_authorization(self):
        report = build_report(_state(stable=True, binding=True, receipts=True), _paper(stable=True))
        self.assertTrue(report["lifecycle"]["completed_canary_evidence_verified"])
        self.assertFalse(report["lifecycle"]["canary_launch_authorized"])
        self.assertFalse(report["live_orders_enabled"])

    def test_all_gates_are_required(self):
        report = build_report(_state(stable=True, binding=True, receipts=True), _paper(stable=True))
        self.assertTrue(report["settlement_evidence"]["ready"])
        self.assertTrue(report["canary_ready"])

    def test_block_stress_cannot_be_bypassed_by_receipts(self):
        report = build_report(_state(stable=False, binding=True, receipts=True), _paper(stable=False))
        self.assertTrue(report["settlement_evidence"]["ready"])
        self.assertFalse(report["canary_ready"])
        self.assertIn("paper_block_stress_unstable", report["hard_blockers"])

    def test_receipt_components_are_independent_gates(self):
        report = build_report(_state(stable=True, binding=True), _paper(stable=True))
        self.assertTrue(report["orderfilled_binding"]["ready"])
        self.assertFalse(report["settlement_evidence"]["ready"])
        self.assertIn("cash_release_receipts_missing_or_unverified", report["hard_blockers"])

    def test_same_ledger_count_is_independent_gate(self):
        state = _state(stable=True, binding=True, receipts=True)
        state["same_ledger_compounding_rows"] = 9
        report = build_report(state, _paper(stable=True))
        self.assertFalse(report["settlement_evidence"]["ready"])
        self.assertIn("same_ledger_compounding_rows_missing_or_not_exactly_ten", report["hard_blockers"])

    def test_synthetic_binding_is_not_admissible(self):
        report = build_report(_state(stable=True, binding=False), _paper(stable=True))
        self.assertFalse(report["settlement_evidence"]["ready"])
        self.assertFalse(report["canary_ready"])
        self.assertIn("onchain_orderfilled_binding_missing_or_unverified", report["hard_blockers"])


if __name__ == "__main__":
    unittest.main()
