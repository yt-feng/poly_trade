import unittest

from analysis.h330_receipt_gated_admission_audit import audit


def state(**overrides):
    base = {
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "owner_scoped_bundle_available": False,
        "owner_scoped_full_settlement_cash_bundle_available": False,
        "balance_continuity_proven": False,
        "cash_release_receipt_rows": 0,
        "settlement_receipt_rows": 0,
    }
    base.update(overrides)
    return base


class H330Tests(unittest.TestCase):
    def test_empty_contract_stays_closed(self):
        result = audit(state())
        self.assertFalse(result["canary_admission_passed"])
        self.assertEqual(result["verified_canary_fills"], 0)

    def test_complete_contract_can_admit(self):
        result = audit(state(
            actual_fills=10,
            verified_canary_fills=10,
            same_ledger_compounding_rows=10,
            owner_scoped_bundle_available=True,
            owner_scoped_full_settlement_cash_bundle_available=True,
            balance_continuity_proven=True,
            cash_release_receipt_rows=10,
            settlement_receipt_rows=10,
            same_ledger_identity_verified=True,
            cash_chain_contiguous=True,
            ledger_reset_count=0,
        ))
        self.assertTrue(result["canary_admission_passed"])

    def test_ten_rows_from_reset_or_unknown_ledger_do_not_admit(self):
        result = audit(state(
            actual_fills=10,
            verified_canary_fills=10,
            same_ledger_compounding_rows=10,
            owner_scoped_bundle_available=True,
            owner_scoped_full_settlement_cash_bundle_available=True,
            balance_continuity_proven=True,
            cash_release_receipt_rows=10,
            settlement_receipt_rows=10,
            same_ledger_identity_verified=True,
            cash_chain_contiguous=True,
            ledger_reset_count=1,
        ))
        self.assertFalse(result["canary_admission_passed"])

    def test_ten_fills_without_contiguous_cash_chain_do_not_admit(self):
        result = audit(state(
            actual_fills=10,
            verified_canary_fills=10,
            same_ledger_compounding_rows=10,
            owner_scoped_bundle_available=True,
            owner_scoped_full_settlement_cash_bundle_available=True,
            balance_continuity_proven=True,
            cash_release_receipt_rows=10,
            settlement_receipt_rows=10,
            same_ledger_identity_verified=True,
            cash_chain_contiguous=False,
            ledger_reset_count=0,
        ))
        self.assertFalse(result["canary_admission_passed"])


if __name__ == "__main__":
    unittest.main()
