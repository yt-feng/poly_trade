from analysis.h464_current_wire_maker_bridge import audit


def test_current_wire_can_reach_maker_row_assembler():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["checks"]["async_trade_id_binding_ready"] is True
    assert result["checks"]["maker_row_has_owner_order_trade_links"] is True


def test_bridge_stays_offline_and_requires_external_receipts():
    result = audit()
    assert result["checks"]["maker_row_is_not_authenticated"] is True
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert "settlement_receipt" in result["required_future_bundle_fields"]
    assert "cash_release_receipt" in result["required_future_bundle_fields"]
