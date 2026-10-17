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


def test_chain_does_not_reset_cash_between_independent_trials():
    events = [event(0, payout="0"), event(1, cost="5", payout="0")]
    result = evaluate_compound_chain(events, required_fills=2)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "compound_chain_not_growth_positive"


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
