from __future__ import annotations

from tools.seal_h698_saved_domain_cash_ledger import build_payload


def test_h698_payload_has_zero_operation_and_fill_counters() -> None:
    report = {"format": "offline-saved-domain-cash-ledger-v1", "ledger_accepted_events": 3}
    descriptor = {"format": "scoped-research-dataset-v1"}
    payload = build_payload(report, descriptor, run_id="1", run_attempt="1", source_commit="a" * 40)
    assert payload["cash_ledger_report"] == report
    assert payload["new_model_fit_calls"] == 0
    assert payload["raw_quote_replays"] == 0
    assert payload["actual_fills"] == 0
    assert payload["live_orders_enabled"] is False
