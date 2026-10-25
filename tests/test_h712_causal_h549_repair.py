from __future__ import annotations

import pandas as pd

from analysis import h712_causal_h549_repair as repair


def test_h712_removes_post_decision_feature_from_exact_h542_contract() -> None:
    assert repair.REMOVED_FEATURE == "btc_move_4m"
    assert repair.REMOVED_FEATURE not in repair.CAUSAL_FEATURES
    assert len(repair.CAUSAL_FEATURES) + 1 == len(repair.h432.FEATURES)
    assert repair.h542._fit.__globals__ is repair.h432.__dict__


def test_h712_candidate_wrapper_anchors_settlement_to_market_start(monkeypatch) -> None:
    monkeypatch.setattr(
        repair,
        "_LEGACY_CANDIDATE_FRAME",
        lambda model, medians, current, slippage: pd.DataFrame(
            {"slug": ["btc-updown-5m-1000"], "entry_ts": [pd.Timestamp("1970-01-01", tz="UTC")]}
        ),
    )
    result = repair._causal_candidate_frame(None, None, pd.DataFrame(), 0.005)
    assert result.loc[0, "settlement_ts"] == pd.Timestamp("1970-01-01 00:21:40+00:00")
    assert result.loc[0, "cash_release_ts"] == pd.Timestamp("1970-01-01 00:36:40+00:00")


def test_h712_profile_is_frozen_h549() -> None:
    assert repair.BASE_FRACTION == 0.55
    assert repair.BOOST_FRACTION == 0.5875
    assert repair.BOOST_AFTER_PROFITS == 1
    assert repair.PARENT_HYPOTHESIS_ID == "H549_BOOST_05875_AFTER_ONE_PROFIT"


def test_h712_patch_restores_bound_functions() -> None:
    original = (repair.h432.FEATURES, repair.h542._fit, repair.h542._candidate_frame)
    saved = repair._patch_h542()
    try:
        assert repair.REMOVED_FEATURE not in repair.h432.FEATURES
        assert repair.h542._candidate_frame is repair._causal_candidate_frame
    finally:
        repair._restore_h542(saved)
    assert repair.h432.FEATURES is original[0]
    assert repair.h542._fit is original[1]
    assert repair.h542._candidate_frame is original[2]
