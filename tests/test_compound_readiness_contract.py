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


def _state(*, stable: bool, binding: bool) -> dict:
    return {
        "actual_fills": 10 if binding else 0,
        "live_orders_enabled": False,
        "receipt_boundary_status": {
            "authenticated_owner_scoped_bundle_available": binding,
            "btc_five_minute_bundle_available": binding,
            "verified_canary_fills": 10 if binding else 0,
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
    def test_all_gates_are_required(self):
        report = build_report(_state(stable=True, binding=True), _paper(stable=True))
        self.assertTrue(report["settlement_evidence"]["ready"])
        self.assertTrue(report["canary_ready"])

    def test_block_stress_cannot_be_bypassed_by_receipts(self):
        report = build_report(_state(stable=False, binding=True), _paper(stable=False))
        self.assertTrue(report["settlement_evidence"]["ready"])
        self.assertFalse(report["canary_ready"])
        self.assertIn("paper_block_stress_unstable", report["hard_blockers"])

    def test_synthetic_binding_is_not_admissible(self):
        report = build_report(_state(stable=True, binding=False), _paper(stable=True))
        self.assertFalse(report["settlement_evidence"]["ready"])
        self.assertFalse(report["canary_ready"])
        self.assertIn("onchain_orderfilled_binding_missing_or_unverified", report["hard_blockers"])


if __name__ == "__main__":
    unittest.main()
