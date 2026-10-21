from analysis.h496_v2_settlement_path_contract import _event
from analysis.validate_v2_settlement_path import validate_v2_path_bundle


def _bundle():
    return {
        "schema": "authenticated-maker-pair-v2-path-bundle-v1",
        "owner": "synthetic-owner",
        "ledger_id": "ledger-1",
        "chain_id": "polygon",
        "events": [_event(i) for i in range(1, 11)],
    }


def test_v2_path_bundle_accepts_ten_complementary_rows_offline_only():
    result = validate_v2_path_bundle(_bundle())
    assert result["offline_consistency_passed"] is True
    assert result["match_paths"] == ["COMPLEMENTARY"]
    assert result["gate_passed"] is False
    assert result["verified_canary_fills"] == 0


def test_v2_path_bundle_requires_ctf_operation_for_mint():
    document = _bundle()
    document["events"] = [_event(i, path="MINT") for i in range(1, 11)]
    document["events"][0].pop("ctf_operation_ref", None)
    result = validate_v2_path_bundle(document)
    assert result["failure_reason"] == "ctf_operation_ref_required"


def test_v2_path_bundle_rejects_unknown_contract_and_bad_clock():
    document = _bundle()
    document["events"][0]["exchange_contract"] = "0xunknown"
    result = validate_v2_path_bundle(document)
    assert result["failure_reason"] == "unsupported_exchange_contract"

    document = _bundle()
    document["events"][0]["cash_available_ts"] = document["events"][0]["payout_ts"] - 1
    result = validate_v2_path_bundle(document)
    assert result["failure_reason"] == "cash_available_before_payout"
