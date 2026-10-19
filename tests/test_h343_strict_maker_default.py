from analysis.h343_strict_maker_default import audit


def test_strict_async_binding_is_default_and_stays_offline():
    result = audit()
    assert result["default_unbound_chain_passed"] is False
    assert result["default_bound_chain_passed"] is True
    assert result["default_bound_rows"] == 10
    assert result["default_bound_gate_passed"] is False
    assert result["receipt_authenticity_verified"] is False
    assert result["same_ledger_compounding_rows"] == 0
