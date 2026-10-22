from analysis.frozen_h600_canary_candidate_manifest import (
    H600_STRATEGY_CONFIG_SHA256,
    H600_STRATEGY_ID,
    H600_STRATEGY_VERSION,
    audit,
    build_manifest,
    validate_manifest,
)


def test_h621_freezes_h600_identity_and_keeps_gate_closed():
    result = audit()
    assert result["all_checks_passed"] is True
    manifest = result["manifest"]
    assert manifest["candidate"]["id"] == H600_STRATEGY_ID
    assert manifest["candidate"]["version"] == H600_STRATEGY_VERSION
    assert manifest["candidate"]["config_sha256"] == H600_STRATEGY_CONFIG_SHA256
    assert manifest["execution_contract"]["required_consecutive_fills"] == 10
    assert manifest["execution_contract"]["same_ledger"] is True
    assert manifest["canary_admission"]["admission_route"] == "auto_trace_bound"
    assert result["validation"]["gate_passed"] is False


def test_h621_rejects_strategy_or_compounding_drift():
    manifest = build_manifest()
    manifest["candidate"]["config_sha256"] = "0" * 64
    assert validate_manifest(manifest)["all_checks_passed"] is False

    manifest = build_manifest()
    manifest["execution_contract"]["no_concatenation"] = False
    assert validate_manifest(manifest)["all_checks_passed"] is False

    manifest = build_manifest()
    manifest["canary_admission"]["required_strategy_profile"] = "h578_online_depth_proxy"
    assert validate_manifest(manifest)["all_checks_passed"] is False

