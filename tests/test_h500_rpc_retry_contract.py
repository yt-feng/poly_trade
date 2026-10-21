from analysis.h500_rpc_retry_contract import audit


def test_h500_retry_contract_is_bounded_and_read_only():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["checks"]["transient_retry_then_success"] is True
    assert result["checks"]["permanent_error_not_retried"] is True
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
