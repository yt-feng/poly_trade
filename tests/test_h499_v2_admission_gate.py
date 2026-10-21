from analysis.h499_v2_admission_gate import audit


def test_h499_v2_path_is_required_and_empty_state_stays_closed():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["h330_result"]["admission_contract"]["v2_path_bundle_validated"] is True
    assert result["h330_result"]["canary_admission_passed"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
