from analysis.h463_current_user_stream_contract import audit


def test_current_wire_contract_distinguishes_pending_and_terminal_states():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["checks"]["matched_trade_is_pending"] is True
    assert result["checks"]["confirmed_trade_is_terminal_fill_candidate"] is True
    assert result["checks"]["failed_trade_is_terminal_nonfill"] is True


def test_owner_bundle_still_requires_settlement_and_cash_checkpoints():
    result = audit()
    required = set(result["required_owner_bundle_fields"])
    assert {"settlement_receipt", "cash_release_receipt", "independent_balance_checkpoint"} <= required
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
