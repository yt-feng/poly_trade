from __future__ import annotations

import json
import sys

import pandas as pd

from analysis import h711_causal_h600_repair as repair


def test_causal_feature_contract_removes_post_decision_feature() -> None:
    assert repair.REMOVED_FEATURE == "btc_move_4m"
    assert repair.REMOVED_FEATURE not in repair.CAUSAL_FEATURES
    assert len(repair.CAUSAL_FEATURES) + 1 == len(repair.h432.FEATURES)
    assert repair.h561._fit.__globals__ is repair.h432.__dict__


def test_candidate_wrapper_uses_market_duration_before_cash_release(monkeypatch) -> None:
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


def test_main_reports_h600_split_names(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(
        repair,
        "evaluate",
        lambda source_root, source_commit, output: {
            "format": "format",
            "hypothesis_id": "hypothesis",
            "feature_rows": 1,
            "selection": {"rows": 1},
            "holdout": {"rows": 1},
            "causal_feature_contract": {},
            "settlement_clock": "clock",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["h711", "--source-root", str(tmp_path), "--output", str(tmp_path / "out")],
    )
    repair.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["selection"] == {"rows": 1}
    assert payload["holdout"] == {"rows": 1}
    assert "variants" not in payload
