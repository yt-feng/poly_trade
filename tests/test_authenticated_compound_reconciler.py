import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from authenticated_compound_reconciler import reconcile_authenticated_chain


def row(i: int, *, owner: str = "owner-1") -> dict[str, object]:
    sequence = i + 1
    balance_before = 10 + i * 0.5
    balance_after = balance_before - 5
    return {
        "owner": owner,
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{i - 1}",
        "status": "confirmed",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
        "released_cash_since_previous_entry": "5.50" if sequence > 1 else "0",
        "balance_before": str(balance_before),
        "balance_after": str(balance_after),
    }


def test_valid_owner_scoped_receipts_form_one_compound_chain():
    result = reconcile_authenticated_chain(
        [row(i) for i in range(10)], owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert result["ending_cash"] == "15.00"
    assert result["unique_balance_checkpoints"] == 10


def test_settled_zero_payout_can_continue_on_same_ledger_when_next_entry_is_fundable():
    rows = [row(i) for i in range(10)]
    cash_after = 10.0
    prior_payout = 0.0
    for i, current in enumerate(rows):
        payout = 0.0 if i == 1 else 6.0
        released = prior_payout
        balance_before = cash_after + released
        balance_after = balance_before - 5.0
        current["payout"] = str(payout)
        current["released_cash_since_previous_entry"] = str(released)
        current["balance_before"] = str(balance_before)
        current["balance_after"] = str(balance_after)
        cash_after = balance_after
        prior_payout = payout
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="14.0",
    )
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert result["ending_cash"] == "14.0"


def test_owner_mismatch_fails_closed_before_cash_replay():
    result = reconcile_authenticated_chain(
        [row(i, owner="other") for i in range(10)], owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "owner_scope_mismatch"
    assert result["identity_complete"] is False


def test_duplicate_transaction_or_checkpoint_cannot_count_twice():
    rows = [row(i) for i in range(10)]
    rows[1]["transaction_ref"] = rows[0]["transaction_ref"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "duplicate_transaction_ref"


def test_missing_checkpoint_is_not_an_authenticated_fill():
    rows = [row(i) for i in range(10)]
    del rows[0]["settlement_receipt_id"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "missing_settlement_receipt_id"


def test_cash_snapshot_must_be_explicitly_reconciled():
    rows = [row(i) for i in range(10)]
    rows[0]["cash_reconciled"] = False
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "cash_reconciliation_required"


def test_balance_checkpoint_must_be_independent():
    rows = [row(i) for i in range(10)]
    rows[0]["independent_checkpoint"] = False
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "independent_balance_checkpoint_required"


def test_cash_release_receipt_is_required_and_distinct_from_settlement():
    rows = [row(i) for i in range(10)]
    del rows[0]["cash_release_receipt_id"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["failure_reason"] == "missing_cash_release_receipt_id"

    rows = [row(i) for i in range(10)]
    rows[0]["cash_release_receipt_id"] = rows[0]["settlement_receipt_id"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["failure_reason"] == "cash_release_receipt_not_independent"


def test_concatenated_reset_ledgers_fail_closed():
    rows = [row(i) for i in range(10)]
    rows[5]["ledger_id"] = "ledger-2"
    result = reconcile_authenticated_chain(rows, owner="owner-1", starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing", closing_balance="15.00")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "ledger_id_mismatch"


def test_two_five_fill_windows_with_sequence_reset_fail_closed():
    """Ten Canary rows cannot be made by joining two separately reset windows."""
    rows = [row(i) for i in range(10)]
    for index in range(5, 10):
        # Keep the owner, ledger, chain and receipt identities unique while
        # resetting only the window-local sequence.  A valid chain must still
        # be exactly 1..10 on one evolving ledger.
        rows[index]["sequence_no"] = index - 4
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "sequence_no_must_be_exactly_one_to_ten"


def test_missing_opening_or_closing_anchor_fails_closed():
    result = reconcile_authenticated_chain([row(i) for i in range(10)], owner="owner-1")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "ledger_anchors_required"


def test_closing_balance_anchor_must_match_one_evolving_ledger():
    result = reconcile_authenticated_chain(
        [row(i) for i in range(10)], owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="10",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "closing_balance_arithmetic_mismatch"


def test_each_release_amount_must_match_prior_payout_schedule():
    rows = [row(i) for i in range(10)]
    # Keep the declared balance chain and closing balance internally
    # consistent with the altered payout.  The release row still says 5.50,
    # so a receipt-unbound reconciler could incorrectly admit this chain.
    rows[0]["payout"] = "6.00"
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.50",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "released_cash_amount_mismatch"


def test_release_timestamp_must_be_before_next_entry_to_fund_it():
    rows = [row(i) for i in range(10)]
    rows[0]["cash_available_ts"] = "1000"
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "released_cash_amount_mismatch"


def test_release_at_same_timestamp_as_its_entry_is_not_admissible():
    rows = [row(i) for i in range(10)]
    rows[0]["cash_available_ts"] = rows[0]["entry_ts"]
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "cash_available_timestamp_must_follow_entry"


def test_prior_release_at_same_timestamp_as_next_entry_is_not_admissible():
    rows = [row(i) for i in range(10)]
    rows[0]["cash_available_ts"] = rows[1]["entry_ts"]
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "cash_available_timestamp_must_precede_next_entry"


def test_prior_release_after_next_entry_is_not_admissible():
    rows = [row(i) for i in range(10)]
    rows[0]["cash_available_ts"] = "250"
    result = reconcile_authenticated_chain(
        rows,
        owner="owner-1",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="15.00",
    )
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "released_cash_amount_mismatch"
