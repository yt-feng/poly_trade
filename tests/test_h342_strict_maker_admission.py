from analysis.h342_strict_maker_admission import audit


def test_strict_admission_requires_async_binding_and_stays_offline():
    result = audit()
    assert result["strict_without_async_binding_passed"] is False
    assert result["strict_with_async_binding_passed"] is True
    assert result["strict_with_async_binding_rows"] == 10
    assert result["partial_binding_passed"] is False
    assert result["receipt_authenticity_verified"] is False
    assert result["same_ledger_compounding_rows"] == 0
    assert result["actual_fills"] == 0
