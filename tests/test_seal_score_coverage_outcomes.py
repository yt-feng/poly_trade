from __future__ import annotations

from tools.seal_score_coverage_outcomes import build_payload


def test_coverage_payload_has_zero_new_compute_and_fills() -> None:
    report = {"format": "offline-score-coverage-diagnostic-v1", "score_rows": 3}
    descriptor = {"format": "scoped-research-dataset-v1", "version": 1}
    payload = build_payload(report, descriptor, run_id="7", run_attempt="1", source_commit="abc")
    assert payload["new_model_fit_calls"] == 0
    assert payload["new_model_predict_calls"] == 0
    assert payload["new_strategy_replays"] == 0
    assert payload["actual_fills"] == 0
    assert payload["coverage_report"] == report
