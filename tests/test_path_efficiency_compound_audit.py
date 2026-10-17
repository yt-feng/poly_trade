import pandas as pd

from analysis.path_efficiency_compound_audit import audit_trade_log, paper_events_from_trade_log


def _row(i, split="holdout59", candidate="v1_adaptive_mix__path_efficiency_ge_050"):
    ts = pd.Timestamp("2026-05-01T00:00:00Z") + pd.Timedelta(minutes=20 * i)
    return {
        "candidate": candidate,
        "split": split,
        "strategy": "v1_adaptive_mix",
        "first_quote_ts": ts.isoformat(),
        "release_ts": (ts + pd.Timedelta(minutes=15)).isoformat(),
        "market_id": f"market-{i}",
        "filled_cost": "5",
        "fee_usd": "0.05",
        "payout_at_settlement": "6.50",
    }


def test_filters_split_and_removes_duplicate_event_identity():
    rows = [_row(i) for i in range(10)]
    rows.append(dict(rows[3]))
    rows.append(_row(99, split="selection20"))
    events, counts = paper_events_from_trade_log(pd.DataFrame(rows))
    assert counts == {"raw_rows": 11, "unique_rows": 10, "duplicate_rows_removed": 1}
    assert len(events) == 10
    assert events[0]["entry_ts"] < events[-1]["entry_ts"]
    assert all(event["status"] == "paper" for event in events)


def test_empty_candidate_split_is_explicitly_zero_events():
    events, counts = paper_events_from_trade_log(pd.DataFrame([_row(1)]), split="missing")
    assert events == []
    assert counts == {"raw_rows": 0, "unique_rows": 0, "duplicate_rows_removed": 0}


def test_required_prefix_is_one_cash_ledger_not_independent_trials(tmp_path):
    rows = [_row(i) for i in range(10)]
    path = tmp_path / "trade_log.csv"
    pd.DataFrame(rows).to_csv(path, index=False)

    result = audit_trade_log(path)
    prefix = result["required_prefix_chain"]
    assert prefix["fills"] == 10
    assert prefix["consecutive"] is True
    assert prefix["evidence_mode"] == "paper_upper_bound"
    assert prefix["paper_replay_passed"] is True
    assert prefix["gate_passed"] is False
    assert prefix["ledger"][0]["cash_before_entry"] == "10"
    assert prefix["ledger"][1]["cash_before_entry"] != "10"
