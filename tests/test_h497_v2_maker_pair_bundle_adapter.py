from analysis.h497_v2_maker_pair_bundle_adapter import audit, _event
from analysis.authenticated_v2_maker_pair_bundle_adapter import assemble_authenticated_v2_maker_pair_event


def test_h497_integrated_adapter_is_offline_only():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["h490_offline_consistency_passed"] is True
    assert result["h496_offline_consistency_passed"] is True
    assert result["paper_pairs"] == 10
    assert result["paper_ending_cash"] == "60"
    assert result["gate_passed"] is False
    assert result["actual_fills"] == 0


def test_h497_rejects_path_field_conflict():
    row = _event(1)
    assert row["v2_path_contract_validated_offline"] is True
