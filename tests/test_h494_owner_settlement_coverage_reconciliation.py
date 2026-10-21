import pytest

from analysis.h494_owner_settlement_coverage_reconciliation import (
    existing_redacted_snapshot,
    reconcile,
)


def test_h494_reconciles_complete_trade_batch_but_partial_settlement_chain():
    result = reconcile(existing_redacted_snapshot())
    assert result["all_checks_passed"] is True
    assert result["coverage_complete_for_existing_trade_batch"] is True
    assert result["authenticated_trade_rows"] == 47
    assert result["settlement_receipt_rows"] == 1
    assert result["redeem_rows"] == 3
    assert result["partial_owner_cash_reuse_links"] == 3
    assert result["full_ten_fill_settlement_cash_chain"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0


def test_h494_fails_closed_on_inconsistent_readback():
    snapshot = existing_redacted_snapshot()
    snapshot["owner_cash_mints"] = 2
    result = reconcile(snapshot)
    assert result["all_checks_passed"] is False
    assert result["failure_reason"] == "existing_coverage_fields_are_inconsistent"
    assert result["verified_canary_fills"] == 0


def test_h494_rejects_non_integer_counts():
    snapshot = existing_redacted_snapshot()
    snapshot["missing"] = "0"
    with pytest.raises(ValueError, match="missing_must_be_nonnegative_integer"):
        reconcile(snapshot)
