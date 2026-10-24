from __future__ import annotations

from tools.seal_h697_candidate_coverage import build_payload


def test_h697_payload_is_aggregate_and_zero_operation() -> None:
    report = {"format": "offline-full-candidate-coverage-v1", "score_rows": 4}
    descriptor = {"format": "scoped-research-dataset-v1"}
    payload = build_payload(report, descriptor, run_id="1", run_attempt="1", source_commit="a" * 40)
    assert payload["coverage_report"] == report
    assert payload["new_model_fit_calls"] == 0
    assert payload["new_strategy_replays"] == 0
    assert payload["actual_fills"] == 0
    assert payload["live_orders_enabled"] is False
