from copy import deepcopy

from analysis.frozen_canary_candidate_manifest import audit, build_manifest, source_parity, validate_manifest


def test_h498_freezes_one_consecutive_compounding_contract():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["manifest"]["execution_contract"]["required_consecutive_fills"] == 10
    assert result["manifest"]["execution_contract"]["same_ledger"] is True
    assert result["manifest"]["execution_contract"]["no_concatenation"] is True
    assert result["manifest"]["canary_admission"]["gate_passed"] is False
    assert result["manifest"]["paper_evidence"]["verified_canary_fills"] == 0
    assert result["source_parity"]["all_literals_match"] is True


def test_h498_source_parity_is_dependency_free_and_complete():
    parity = source_parity()
    assert parity["all_literals_match"] is True
    assert parity["mismatches"] == []


def test_h498_fails_closed_on_parameter_or_promotion_drift():
    manifest = deepcopy(build_manifest())
    manifest["execution_contract"]["minimum_order_usd"] = 1.0
    manifest["live_orders_enabled"] = True
    result = validate_manifest(manifest)
    assert result["all_checks_passed"] is False
    assert "execution_contract.minimum_order_usd" in result["failures"]
    assert "live_orders_enabled" in result["failures"]

