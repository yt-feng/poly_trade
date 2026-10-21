import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from maker_pair_receipt_contract import validate_maker_pair_chain


def pair(i: int) -> dict[str, object]:
    return {
        "owner": "owner-1",
        "status": "confirmed",
        "sequence_no": i + 1,
        "pair_id": f"pair-{i}",
        "up_order_id": f"up-order-{i}",
        "down_order_id": f"down-order-{i}",
        "up_trade_id": f"up-trade-{i}",
        "down_trade_id": f"down-trade-{i}",
        "up_transaction_ref": f"up-tx-{i}",
        "down_transaction_ref": f"down-tx-{i}",
        "up_trader_side": "MAKER",
        "down_trader_side": "MAKER",
        "up_order_type": "GTC",
        "down_order_type": "GTC",
        "up_post_only": True,
        "down_post_only": True,
        "up_order_status": "MATCHED",
        "down_order_status": "MATCHED",
        "up_trade_status": "TRADE_STATUS_CONFIRMED",
        "down_trade_status": "TRADE_STATUS_CONFIRMED",
        "up_order_acceptance_ts": str(i * 100 - 20),
        "down_order_acceptance_ts": str(i * 100 - 20),
        "up_trade_matched_ts": str(i * 100),
        "down_trade_matched_ts": str(i * 100),
        "up_fee_rate_bps": "0",
        "down_fee_rate_bps": "0",
        "merge_receipt_id": f"merge-{i}",
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


def test_ten_complete_maker_pairs_pass_one_cash_ledger():
    result = validate_maker_pair_chain([pair(i) for i in range(10)], owner="owner-1")
    assert result["offline_consistency_passed"] is True
    assert result["gate_passed"] is False
    assert result["receipt_authenticity_verified"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
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


def test_each_leg_requires_post_only_confirmed_maker_lifecycle():
    row = pair(0)
    row["down_post_only"] = False
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["failure_reason"] == "down_post_only_required"

    row = pair(0)
    row["up_trade_status"] = "TRADE_STATUS_MATCHED"
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["failure_reason"] == "up_trade_confirmation_required"


def test_each_leg_must_be_accepted_and_matched_before_entry():
    row = pair(0)
    row["up_order_acceptance_ts"] = "1"
    row["entry_ts"] = "0"
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["failure_reason"] == "up_order_acceptance_after_entry"

    row = pair(0)
    row["down_trade_matched_ts"] = "1"
    row["entry_ts"] = "0"
    result = validate_maker_pair_chain([row], owner="owner-1")
    assert result["failure_reason"] == "down_trade_match_after_entry"
