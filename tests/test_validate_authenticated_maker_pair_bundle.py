import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from validate_authenticated_maker_pair_bundle import validate_bundle_document


def pair(index: int) -> dict[str, object]:
    seq = index + 1
    before = Decimal("10") + Decimal("5") * index
    after = before - Decimal("5")
    return {
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "sequence_no": seq,
        "previous_checkpoint_id": "opening" if index == 0 else f"balance-{index}",
        "status": "confirmed",
        "pair_id": f"pair-{seq}",
        "up_order_id": f"up-order-{seq}",
        "down_order_id": f"down-order-{seq}",
        "up_trade_id": f"up-trade-{seq}",
        "down_trade_id": f"down-trade-{seq}",
        "up_transaction_ref": f"up-tx-{seq}",
        "down_transaction_ref": f"down-tx-{seq}",
        "up_order_type": "GTC",
        "down_order_type": "GTC",
        "up_post_only": True,
        "down_post_only": True,
        "up_order_status": "MATCHED",
        "down_order_status": "MATCHED",
        "up_trade_status": "TRADE_STATUS_CONFIRMED",
        "down_trade_status": "TRADE_STATUS_CONFIRMED",
        "up_trader_side": "MAKER",
        "down_trader_side": "MAKER",
        "up_order_acceptance_ts": seq * 100 - 20,
        "down_order_acceptance_ts": seq * 100 - 20,
        "up_trade_matched_ts": seq * 100,
        "down_trade_matched_ts": seq * 100,
        "up_fee_rate_bps": "0",
        "down_fee_rate_bps": "0",
        "up_shares": "10",
        "down_shares": "10",
        "up_price": "0.25",
        "down_price": "0.25",
        "merge_receipt_id": f"merge-{seq}",
        "settlement_receipt_id": f"settlement-{seq}",
        "cash_release_receipt_id": f"cash-release-{seq}",
        "balance_checkpoint_id": f"balance-{seq}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": seq * 100,
        "cash_available_ts": seq * 100 + 10,
        "released_cash_since_previous_entry": "0" if index == 0 else "10",
        "balance_before": str(before),
        "balance_after": str(after),
        "entry_cost": "5",
        "fee": "0",
        "payout": "10",
    }


def document() -> dict[str, object]:
    return {
        "schema": "authenticated-maker-pair-bundle-v1",
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "starting_checkpoint_id": "opening",
        "closing_balance_checkpoint_id": "closing",
        "initial_cash": "10",
        "closing_balance": "60",
        "events": [pair(i) for i in range(10)],
    }


def test_ten_pairs_are_one_offline_compounding_chain_but_not_a_canary():
    result = validate_bundle_document(document())
    assert result["offline_consistency_passed"] is True
    assert result["gate_passed"] is False
    assert result["receipt_authenticity_verified"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["pairs"] == 10
    assert result["ending_cash"] == "60"
    assert result["unique_order_ids"] == 20
    assert result["unique_trade_ids"] == 20


def test_missing_second_leg_fails_closed():
    payload = document()
    payload["events"][0].pop("down_trade_id")
    result = validate_bundle_document(payload)
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "down_trade_id_required"


def test_post_only_and_confirmation_are_required_for_each_leg():
    payload = document()
    payload["events"][0]["down_post_only"] = False
    assert validate_bundle_document(payload)["failure_reason"] == "down_post_only_required"

    payload = document()
    payload["events"][0]["up_trade_status"] = "MATCHED"
    assert validate_bundle_document(payload)["failure_reason"] == "up_trade_confirmation_required"


def test_cash_release_must_precede_next_entry_on_same_ledger():
    payload = document()
    payload["events"][0]["cash_available_ts"] = payload["events"][1]["entry_ts"]
    result = validate_bundle_document(payload)
    assert result["failure_reason"] == "prior_cash_release_must_precede_next_entry"


def test_reset_ledger_and_duplicate_pair_id_cannot_be_concatenated():
    payload = document()
    payload["events"][5]["ledger_id"] = "ledger-2"
    assert validate_bundle_document(payload)["failure_reason"] == "ledger_id_mismatch"

    payload = document()
    payload["events"][1]["pair_id"] = payload["events"][0]["pair_id"]
    assert validate_bundle_document(payload)["failure_reason"] == "duplicate_pair_id"

