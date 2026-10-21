from decimal import Decimal

import pytest

from analysis.authenticated_maker_pair_bundle_adapter import (
    assemble_authenticated_maker_pair_event,
)
from analysis.validate_authenticated_maker_pair_bundle import validate_bundle_document


def leg(prefix: str, sequence: int, *, owner: str = "owner-1", market: str = "market-1"):
    order_id = f"{prefix}-order-{sequence}"
    trade_id = f"{prefix}-trade-{sequence}"
    return (
        {
            "order_id": order_id,
            "owner": owner,
            "order_type": "GTC",
            "post_only": True,
            "order_status": "MATCHED",
            "order_acceptance_ts": sequence * 100 - 20,
        },
        {
            "owner": owner,
            "trade_owner": owner,
            "trade_id": trade_id,
            "transaction_ref": f"{prefix}-tx-{sequence}",
            "market_id": market,
            "asset_id": f"{prefix}-asset-{sequence}",
            "shares": "10",
            "price": "0.25",
            "fee_rate_bps": "0",
            "side": "BUY",
            "execution_role": "MAKER",
            "trader_side": "MAKER",
            "trade_status": "TRADE_STATUS_CONFIRMED",
            "trade_matched_ts": sequence * 100,
            "maker_order_ids": [order_id],
        },
    )


def pair_event(sequence: int):
    up_order, up_trade = leg("up", sequence)
    down_order, down_trade = leg("down", sequence)
    before = Decimal("10") + Decimal("5") * (sequence - 1)
    pair = {
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "market_id": "market-1",
        "pair_id": f"pair-{sequence}",
        "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}",
        "entry_ts": sequence * 100,
        "cash_available_ts": sequence * 100 + 10,
        "released_cash_since_previous_entry": "0" if sequence == 1 else "10",
        "balance_before": str(before),
        "balance_after": str(before - Decimal("5")),
        "entry_cost": "5",
        "fee": "0",
        "payout": "10",
        "balance_checkpoint_id": f"balance-{sequence}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
    }
    settlement = {
        "merge_receipt_id": f"merge-{sequence}",
        "settlement_receipt_id": f"settlement-{sequence}",
        "cash_release_receipt_id": f"cash-{sequence}",
    }
    return assemble_authenticated_maker_pair_event(
        up_order_event=up_order,
        up_trade_event=up_trade,
        down_order_event=down_order,
        down_trade_event=down_trade,
        pair_fields=pair,
        settlement_fields=settlement,
    )


def bundle():
    return {
        "schema": "authenticated-maker-pair-bundle-v1",
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "starting_checkpoint_id": "opening",
        "closing_balance_checkpoint_id": "closing",
        "initial_cash": "10",
        "closing_balance": "60",
        "events": [pair_event(i) for i in range(1, 11)],
    }


def test_adapter_builds_ten_pair_bundle_for_offline_validation_only():
    document = bundle()
    result = validate_bundle_document(document)
    assert result["offline_consistency_passed"] is True
    assert result["gate_passed"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["unique_order_ids"] == 20


def test_adapter_rejects_late_trade_confirmation_and_nonmaker_leg():
    up_order, up_trade = leg("up", 1)
    down_order, down_trade = leg("down", 1)
    pair = pair_event(1)
    down_trade["trade_status"] = "MATCHED"
    with pytest.raises(ValueError, match="down_trade_confirmation_required"):
        assemble_authenticated_maker_pair_event(
            up_order_event=up_order,
            up_trade_event=up_trade,
            down_order_event=down_order,
            down_trade_event=down_trade,
            pair_fields=pair,
            settlement_fields={
                "merge_receipt_id": "merge-x",
                "settlement_receipt_id": "settlement-x",
                "cash_release_receipt_id": "cash-x",
            },
        )

    up_order, up_trade = leg("up", 1)
    down_order, down_trade = leg("down", 1)
    up_trade["trade_matched_ts"] = 101
    with pytest.raises(ValueError, match="up_trade_match_after_entry"):
        assemble_authenticated_maker_pair_event(
            up_order_event=up_order,
            up_trade_event=up_trade,
            down_order_event=down_order,
            down_trade_event=down_trade,
            pair_fields=pair,
            settlement_fields={
                "merge_receipt_id": "merge-x",
                "settlement_receipt_id": "settlement-x",
                "cash_release_receipt_id": "cash-x",
            },
        )


def test_adapter_rejects_cross_owner_and_duplicate_assets():
    up_order, up_trade = leg("up", 1)
    down_order, down_trade = leg("down", 1, owner="other-owner")
    with pytest.raises(ValueError, match="down_owner_scope_mismatch"):
        assemble_authenticated_maker_pair_event(
            up_order_event=up_order,
            up_trade_event=up_trade,
            down_order_event=down_order,
            down_trade_event=down_trade,
            pair_fields=pair_event(1),
            settlement_fields={
                "merge_receipt_id": "merge-x",
                "settlement_receipt_id": "settlement-x",
                "cash_release_receipt_id": "cash-x",
            },
        )

    up_order, up_trade = leg("up", 1)
    down_order, down_trade = leg("down", 1)
    down_trade["asset_id"] = up_trade["asset_id"]
    with pytest.raises(ValueError, match="paired_assets_must_differ"):
        assemble_authenticated_maker_pair_event(
            up_order_event=up_order,
            up_trade_event=up_trade,
            down_order_event=down_order,
            down_trade_event=down_trade,
            pair_fields=pair_event(1),
            settlement_fields={
                "merge_receipt_id": "merge-x",
                "settlement_receipt_id": "settlement-x",
                "cash_release_receipt_id": "cash-x",
            },
        )

