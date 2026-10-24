from __future__ import annotations

import pandas as pd

from analysis.offline_full_candidate_coverage import build_report, implied_exit_bid


def _config() -> dict:
    return {
        "base_protocol": {
            "run_order": ["train", "test"],
            "minimum_training_runs": 1,
            "fee_rate": 0.07,
            "slippage": 0.01,
            "edge_floor": 0.02,
        },
        "expected_test_cohort": {"scored_rows": 2},
    }


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    scores = pd.DataFrame([
        {"market_id": "test::a", "run_name": "test", "slug": "a", "side": "up",
         "decision_ts": "2026-01-01T00:00:00+00:00", "market_close_ts": "2026-01-01T00:05:00+00:00",
         "fit_run": "test", "model_status": "fitted", "predicted_net_margin_per_share": "0.03"},
        {"market_id": "test::b", "run_name": "test", "slug": "b", "side": "down",
         "decision_ts": "2026-01-01T00:05:00+00:00", "market_close_ts": "2026-01-01T00:10:00+00:00",
         "fit_run": "test", "model_status": "fitted", "predicted_net_margin_per_share": "0.01"},
    ])
    features = pd.DataFrame([
        {"market_id": "train::ignored", "run_name": "train", "slug": "ignored", "side": "up",
         "decision_ts": "2025-12-31T23:55:00+00:00", "market_close_ts": "2026-01-01T00:00:00+00:00",
         "decision_ask_price": "0.40", "decision_sell_size": "10"},
        {"market_id": "test::a", "run_name": "test", "slug": "a", "side": "up",
         "decision_ts": "2026-01-01T00:00:00+00:00", "market_close_ts": "2026-01-01T00:05:00+00:00",
         "decision_ask_price": "0.40", "decision_sell_size": "10"},
        {"market_id": "test::b", "run_name": "test", "slug": "b", "side": "down",
         "decision_ts": "2026-01-01T00:05:00+00:00", "market_close_ts": "2026-01-01T00:10:00+00:00",
         "decision_ask_price": "0.45", "decision_sell_size": "0"},
    ])
    return features, scores


def test_margin_inversion_round_trips_frozen_cash_equation() -> None:
    bid, margin = implied_exit_bid(0.03, 0.40, fee_rate=0.07, slippage=0.01)
    assert 0 < bid < 1
    assert abs(margin - 0.03) < 1e-12


def test_full_saved_score_coverage_is_aggregate_only() -> None:
    features, scores = _frames()
    report = build_report(features, scores, _config(), feature_sha256="f" * 64, score_sha256="s" * 64)
    assert report["test_cohort_rows"] == 2
    assert report["score_rows_at_or_above_frozen_edge_floor"] == 1
    assert report["score_rows_at_or_above_floor_with_execution_projection"] == 1
    assert report["maximum_margin_inversion_absolute_error"] < 1e-12
    assert "market_ids" not in report
    assert report["model_fit_calls"] == 0
    assert report["strategy_replays"] == 0
