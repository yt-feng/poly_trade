import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from maker_pair_receipt_contract import validate_maker_pair_chain


def pair(i: int) -> dict[str, object]:
    return {
        "owner": "owner-1",
        "status": "confirmed",
        "pair_id": f"pair-{i}",
        "up_trade_id": f"up-trade-{i}",
        "down_trade_id": f"down-trade-{i}",
        "up_transaction_ref": f"up-tx-{i}",
        "down_transaction_ref": f"down-tx-{i}",
        "up_trader_side": "MAKER",
        "down_trader_side": "MAKER",
        "up_fee_rate_bps": "0",
        "down_fee_rate_bps": "0",
        "merge_receipt_id": f"merge-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
    }


def test_ten_complete_maker_pairs_pass_one_cash_ledger():
    result = validate_maker_pair_chain([pair(i) for i in range(10)], owner="owner-1")
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert result["ending_cash"] == "15.00"


def test_single_leg_or_missing_merge_receipt_fails_closed():
    row = pair(0)
    row.pop("down_trade_id")
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "missing_down_trade_id"

    row = pair(0)
    row.pop("merge_receipt_id")
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "missing_merge_receipt_id"


def test_nonmaker_leg_and_duplicate_pair_identity_fail_closed():
    row = pair(0)
    row["down_trader_side"] = "TAKER"
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "maker_side_not_confirmed"

    first = pair(0)
    second = pair(1)
    second["merge_receipt_id"] = first["merge_receipt_id"]
    result = validate_maker_pair_chain([first, second], owner="owner-1")
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "duplicate_pair_identity"
