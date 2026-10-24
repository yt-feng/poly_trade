from __future__ import annotations

import pandas as pd

from analysis.offline_full_candidate_coverage import implied_exit_bid
from analysis.offline_saved_domain_cash_ledger import build_report


def _config() -> dict:
    return {"base_protocol": {
        "run_order": ["train", "test"], "minimum_training_runs": 1,
        "fee_rate": 0.07, "slippage": 0.01, "edge_floor": 0.02,
        "initial_cash": 10.0, "minimum_order_usd": 5.0,
        "minimum_order_shares": 5.0, "depth_thresholds": [3.0, 5.0, 7.0],
        "entry_delay_seconds": 4.0, "latency_seconds": 8.0,
        "required_fills": 10,
    }, "expected_test_cohort": {"scored_rows": 1}}


def _frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    score = 0.03
    predicted, _ = implied_exit_bid(score, 0.40, fee_rate=0.07, slippage=0.01)
    features = pd.DataFrame([{"market_id": "test::a", "run_name": "test", "slug": "a", "side": "up",
        "decision_ts": "2026-01-01T00:00:00+00:00", "market_close_ts": "2026-01-01T00:05:00+00:00",
        "decision_ask_price": "0.40", "decision_sell_size": "10"}])
    scores = pd.DataFrame([{"market_id": "test::a", "run_name": "test", "slug": "a", "side": "up",
        "decision_ts": "2026-01-01T00:00:00+00:00", "market_close_ts": "2026-01-01T00:05:00+00:00",
        "fit_run": "test", "model_status": "fitted", "predicted_net_margin_per_share": str(score)}])
    domains = pd.DataFrame([{"market_id": "test::a", "run_name": "test", "slug": "a", "side": "up",
        "decision_ts": "2026-01-01T00:00:00+00:00", "market_close_ts": "2026-01-01T00:05:00+00:00",
        "entry_quote_status": "selected", "entry_quote_ts": "2026-01-01T00:00:13+00:00",
        "entry_ask_price": "0.40", "entry_available_shares": "20", "entry_full_book_valid": "True", "entry_size_valid": "True",
        "exit_quote_status": "selected", "exit_quote_ts": "2026-01-01T00:00:55+00:00", "exit_bid_price": str(predicted - .01), "exit_available_shares": "20",
        "exit_full_book_valid": "True", "exit_size_valid": "True", "paired_high_quality_quotes": "True",
        "entry_fill_price": "0.41", "exit_fill_price": str(predicted - .01), "entry_cash_cost_per_share": "0.4268",
        "exit_net_proceeds_per_share": "0.50", "net_cash_difference_per_share": "0.0732",
        "entry_effective_available_shares": "20", "exit_effective_available_shares": "20",
        "minimum_entry_shares": "5", "minimum_entry_cash": "2.134", "maximum_pair_capacity_shares": "20",
        "simulation_cash_domain_observed": "True", "minimum_order_has_pair_capacity": "True",
        "cash_domain_label_available_ts": "2026-01-01T00:00:55+00:00", "assumed_cash_release_ts": "2026-01-01T00:05:00+00:00"}])
    return features, domains, scores


def test_saved_domain_ledger_reports_event_without_canary_claim() -> None:
    features, domains, scores = _frames()
    report = build_report(features, domains, scores, _config(), feature_sha256="f" * 64,
                          domain_sha256="d" * 64, score_sha256="s" * 64)
    assert report["selected_score_candidates"] == 1
    assert report["ledger_accepted_events"] == 1
    assert report["ledger_resolved_events"] == 1
    assert report["continuous_required_sequence_complete"] is False
    assert report["actual_fills"] == 0
    assert report["raw_quote_replays"] == 0
