import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from maker_pair_cash_reconciliation import reconcile_pair_cash


ASSET = "fixture:eip155:137/erc20:USDC"


def pair():
    return {"pair_status": "settlement_candidate", "pair_trade_ids": ["u", "d"]}


def checkpoints():
    opening = {"cash_asset": ASSET, "account_id": "acct", "balance_checkpoint_id": "open", "observed_ms": 100, "balance_amount": "20"}
    flows = [
        {"event_id": "f-u", "economic_id": "u", "kind": "entry_fill", "account_id": "acct", "cash_asset": ASSET, "status": "CONFIRMED", "transaction_ref": "tx-u", "observed_ms": 200, "cash_delta_amount": "-5"},
        {"event_id": "f-d", "economic_id": "d", "kind": "entry_fill", "account_id": "acct", "cash_asset": ASSET, "status": "CONFIRMED", "transaction_ref": "tx-d", "observed_ms": 201, "cash_delta_amount": "-8"},
        {"event_id": "merge", "economic_id": "merge-1", "kind": "merge", "account_id": "acct", "cash_asset": ASSET, "status": "CONFIRMED", "transaction_ref": "tx-m", "observed_ms": 202, "cash_delta_amount": "20"},
    ]
    closing = {"cash_asset": ASSET, "account_id": "acct", "balance_checkpoint_id": "close", "observed_ms": 300, "balance_amount": "27", "reserved_cash_amount": "0", "available_cash_amount": "27", "complete_cash_flow_slice": True, "covered_event_ids": ["f-u", "f-d", "merge"], "position_checkpoint_id": "pos-close", "reconciled_position_shares": "0"}
    return opening, flows, closing


def test_pair_cash_requires_full_reconciled_slice():
    opening, flows, closing = checkpoints()
    result = reconcile_pair_cash(pair(), opening_checkpoint=opening, cash_flow_evidence=flows, closing_checkpoint=closing, expected_asset=ASSET)
    assert result["cash_reusable"] is True
    assert result["amount_verified"] is True
    assert result["reason"] == "PAIR_SETTLEMENT_AND_CASH_CHECKPOINT_RECONCILED"


def test_pair_cash_rejects_missing_pair_flow_or_amount_mismatch():
    opening, flows, closing = checkpoints()
    result = reconcile_pair_cash(pair(), opening_checkpoint=opening, cash_flow_evidence=flows[1:], closing_checkpoint=closing, expected_asset=ASSET)
    assert result["cash_reusable"] is False
    assert result["reason"] == "PAIR_TRADES_MISSING_FROM_CASH_SLICE"
    closing["balance_amount"] = "100"
    closing["available_cash_amount"] = "100"
    result = reconcile_pair_cash(pair(), opening_checkpoint=opening, cash_flow_evidence=flows, closing_checkpoint=closing, expected_asset=ASSET)
    assert result["cash_reusable"] is False
    assert result["reason"] == "CASH_AMOUNT_AUDIT_FAILED"


def test_pair_cash_rejects_reserved_or_insufficient_cash():
    opening, flows, closing = checkpoints()
    closing["reserved_cash_amount"] = "3"
    closing["available_cash_amount"] = "24"
    result = reconcile_pair_cash(pair(), opening_checkpoint=opening, cash_flow_evidence=flows, closing_checkpoint=closing, expected_asset=ASSET)
    assert result["cash_reusable"] is False
    assert result["reason"] == "CASH_REMAINS_RESERVED"
    closing["reserved_cash_amount"] = "0"
    closing["available_cash_amount"] = "4"
    closing["balance_amount"] = "4"
    result = reconcile_pair_cash(pair(), opening_checkpoint=opening, cash_flow_evidence=flows, closing_checkpoint=closing, expected_asset=ASSET)
    assert result["cash_reusable"] is False
    assert result["reason"] == "CASH_AMOUNT_AUDIT_FAILED"
