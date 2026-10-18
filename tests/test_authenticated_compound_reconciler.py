import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from authenticated_compound_reconciler import reconcile_authenticated_chain


def row(i: int, *, owner: str = "owner-1") -> dict[str, object]:
    return {
        "owner": owner,
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
    }


def test_valid_owner_scoped_receipts_form_one_compound_chain():
    result = reconcile_authenticated_chain([row(i) for i in range(10)], owner="owner-1")
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert result["ending_cash"] == "15.00"
    assert result["unique_balance_checkpoints"] == 10


def test_owner_mismatch_fails_closed_before_cash_replay():
    result = reconcile_authenticated_chain([row(0, owner="other")], owner="owner-1")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "owner_scope_mismatch"
    assert result["identity_complete"] is False


def test_duplicate_transaction_or_checkpoint_cannot_count_twice():
    rows = [row(0), row(1)]
    rows[1]["transaction_ref"] = rows[0]["transaction_ref"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", required_fills=2)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "duplicate_transaction_ref"


def test_missing_checkpoint_is_not_an_authenticated_fill():
    rows = [row(0)]
    del rows[0]["settlement_receipt_id"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", required_fills=1)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "missing_settlement_receipt_id"


def test_cash_release_receipt_is_required_and_distinct_from_settlement():
    rows = [row(0)]
    del rows[0]["cash_release_receipt_id"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", required_fills=1)
    assert result["failure_reason"] == "missing_cash_release_receipt_id"

    rows = [row(0)]
    rows[0]["cash_release_receipt_id"] = rows[0]["settlement_receipt_id"]
    result = reconcile_authenticated_chain(rows, owner="owner-1", required_fills=1)
    assert result["failure_reason"] == "cash_release_receipt_not_independent"
