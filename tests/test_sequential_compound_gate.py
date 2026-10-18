import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from sequential_compound_gate import evaluate_compound_chain


def event(i: int, *, status: str = "confirmed", cost: str = "5", payout: str = "5.50"):
    return {
        "status": status,
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": cost,
        "fee": "0",
        "payout": payout,
    }


def test_ten_fills_are_one_growth_positive_reusable_cash_chain():
    result = evaluate_compound_chain([event(i) for i in range(10)])
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert result["ending_cash"] == "15.00"


def test_bounded_chain_stops_at_required_fill_and_ignores_later_trades():
    # The first ten entries are a valid finite batch. Two later losses would
    # make the unbounded history fail, but they are outside this Canary batch.
    events = [event(i) for i in range(10)]
    events.extend([event(10, payout="0"), event(11, payout="0")])
    result = evaluate_compound_chain(
        events,
        stop_after_required_fills=True,
        evidence_mode="paper_upper_bound",
    )
    assert result["fills"] == 10
    assert result["stopped_after_required_fills"] is True
    assert result["ending_cash"] == "15.00"
    assert result["paper_replay_passed"] is True


def test_unbounded_history_still_includes_later_trades():
    events = [event(i) for i in range(10)]
    events.extend([event(10, payout="0"), event(11, payout="0")])
    result = evaluate_compound_chain(events, evidence_mode="paper_upper_bound")
    assert result["fills"] == 12
    assert result["stopped_after_required_fills"] is False
    assert result["paper_replay_passed"] is False


def test_chain_does_not_reset_cash_between_independent_trials():
    events = [event(0, payout="0"), event(1, cost="5", payout="0")]
    result = evaluate_compound_chain(events, required_fills=2)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "compound_chain_not_growth_positive"


def test_resetting_cash_between_fills_cannot_hide_an_unfunded_next_entry():
    # A reset-per-trial calculation would fund the second $6 entry from $10.
    # The real chain has only $5 after the first loss, so it must stop there.
    events = [event(0, payout="0"), event(1, cost="6", payout="7")]
    result = evaluate_compound_chain(events, required_fills=2)
    assert result["fills"] == 1
    assert result["failure_reason"] == "insufficient_cash_for_next_entry"


def test_unconfirmed_event_breaks_the_chain():
    result = evaluate_compound_chain([event(0), event(1, status="paper")])
    assert result["gate_passed"] is False
    assert result["fills"] == 1
    assert result["failure_reason"] == "unconfirmed_fill"


def test_paper_upper_bound_never_becomes_canary_evidence():
    result = evaluate_compound_chain(
        [event(i) for i in range(10)], evidence_mode="paper_upper_bound"
    )
    assert result["paper_replay_passed"] is True
    assert result["gate_passed"] is False


def test_authenticated_chain_requires_independent_settlement_and_balance_evidence():
    row = event(0)
    del row["balance_checkpoint_id"]
    result = evaluate_compound_chain([row], required_fills=1)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "missing_authoritative_receipt_or_checkpoint"


def test_authenticated_chain_rejects_unreconciled_cash_checkpoint():
    row = event(0)
    row["cash_reconciled"] = False
    result = evaluate_compound_chain([row], required_fills=1)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "cash_checkpoint_not_reconciled"


def test_explicit_cash_release_gate_requires_distinct_receipt():
    row = event(0)
    del row["cash_release_receipt_id"]
    result = evaluate_compound_chain(
        [row], required_fills=1, require_cash_release_receipt=True
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "missing_authoritative_receipt_or_checkpoint"

    row["cash_release_receipt_id"] = row["settlement_receipt_id"]
    result = evaluate_compound_chain(
        [row], required_fills=1, require_cash_release_receipt=True
    )
    assert result["failure_reason"] == "cash_release_receipt_not_independent"


def test_overlapping_entries_use_one_cash_ledger_and_are_compounding():
    first = event(0)
    second = event(1)
    second["entry_ts"] = "5"
    second["entry_cost"] = "4"
    result = evaluate_compound_chain([first, second], required_fills=2, minimum_order="4")
    assert result["gate_passed"] is True


def test_events_must_be_chronological():
    first = event(1)
    second = event(0)
    result = evaluate_compound_chain([first, second], required_fills=2)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "events_not_chronological"


def test_equal_entry_timestamps_are_not_a_chronological_chain():
    first = event(0)
    second = event(1)
    second["entry_ts"] = first["entry_ts"]
    result = evaluate_compound_chain([first, second], required_fills=2)
    assert result["gate_passed"] is False
    assert result["fills"] == 1
    assert result["failure_reason"] == "events_not_chronological"


def test_two_reset_ledgers_cannot_be_concatenated_when_identity_is_present():
    first = event(0)
    second = event(1)
    for row in (first, second):
        row["ledger_id"] = "ledger-1"
        row["chain_id"] = "chain-1"
    second["ledger_id"] = "ledger-2"
    result = evaluate_compound_chain(
        [first, second], required_fills=2, evidence_mode="paper_upper_bound"
    )
    assert result["fills"] == 1
    assert result["failure_reason"] == "ledger_id_mismatch"


def test_partial_ledger_identity_is_not_silently_ignored():
    row = event(0)
    row["ledger_id"] = "ledger-1"
    result = evaluate_compound_chain([row], required_fills=1, evidence_mode="paper_upper_bound")
    assert result["fills"] == 0
    assert result["failure_reason"] == "ledger_identity_incomplete"
