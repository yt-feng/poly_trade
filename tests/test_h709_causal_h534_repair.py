from analysis import h709_causal_h534_repair as h709


def test_h709_removes_post_decision_feature_without_changing_parent_parameters():
    assert h709.REMOVED_FEATURE == "btc_move_4m"
    assert h709.REMOVED_FEATURE not in h709.CAUSAL_FEATURES
    assert len(h709.CAUSAL_FEATURES) == len(h709.legacy.FEATURES) - 1
    assert h709.legacy.MODEL_PARAMS["random_state"] == 4121
    assert h709.legacy.ENTRY_SECONDS_AFTER_START == 124.0


def test_h709_uses_market_close_before_cash_release():
    assert h709.MARKET_DURATION_SECONDS == 300.0
    assert h709.legacy.CASH_RELEASE_SECONDS == 900.0
