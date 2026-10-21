from analysis.h488_strict_paired_maker_contract import audit


def test_h488_repairs_pair_gate_without_promoting_synthetic_evidence():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["offline_consistency_passed"] is True
    assert result["paper_pairs"] == 10
    assert result["gate_passed"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
