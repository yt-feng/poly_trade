from analysis.h495_official_settlement_path_review import review


def test_h495_binds_all_v2_settlement_paths_and_keeps_gate_closed():
    result = review()
    assert result["source_version"] == "Polymarket CTF Exchange V2"
    assert result["match_paths"] == ["COMPLEMENTARY", "MINT", "MERGE"]
    assert result["maker_order_filled_emitted_before_batch_ctf_operation"] is True
    assert result["batch_ctf_operations"] == ["SPLIT", "MERGE"]
    assert "owner_scoped_redeem_or_payout_receipt" in result["future_owner_bundle_required_fields"]
    assert result["canary_gate_passed"] is False
    assert result["actual_fills"] == 0
