from analysis.h341_async_maker_bundle_link import link_confirmed_maker_trade
from analysis.maker_compound_bundle_adapter import assemble_maker_compound_row


def order_update(trade_ids):
    return {
        "kind": "entry_order_accepted",
        "order_id": "maker-order-1",
        "owner": "owner-key",
        "trade_ids": list(trade_ids),
    }


def confirmed_trade(**updates):
    value = {
        "kind": "entry_fill",
        "owner": "owner-key",
        "trade_owner": "owner-key",
        "trade_id": "trade-1",
        "transaction_ref": "0xtx-1",
        "trade_status": "TRADE_STATUS_CONFIRMED",
        "maker_order_ids": ["maker-order-1"],
    }
    value.update(updates)
    return value


def test_initial_empty_trade_ids_need_a_later_matching_update():
    result = link_confirmed_maker_trade(
        [order_update([]), order_update(["trade-1"])], confirmed_trade()
    )
    assert result["async_binding_ready"] is True
    assert result["confirmed_fill_event_preserved"] is True
    assert result["fill_count"] == 0


def test_mismatched_async_trade_id_fails_closed():
    result = link_confirmed_maker_trade(
        [order_update([]), order_update(["trade-other"])], confirmed_trade()
    )
    assert result["async_binding_ready"] is False
    assert "confirmed_trade_id_not_preserved_by_order_updates" in result["issues"]


def test_missing_transaction_or_maker_order_fails_closed():
    result = link_confirmed_maker_trade(
        [order_update(["trade-1"])],
        confirmed_trade(transaction_ref="", maker_order_ids=[]),
    )
    assert result["async_binding_ready"] is False
    assert "transaction_ref_required" in result["issues"]
    assert "maker_order_ids_required" in result["issues"]


def test_bundle_adapter_can_require_async_binding_without_counting_fill():
    # Keep the existing offline row fixtures small; the adapter checks the
    # async link before assembling the same ledger row.
    row = assemble_maker_compound_row(
        order_event={
            "kind": "entry_order_accepted",
            "order_id": "maker-order-1",
            "order_status": "MATCHED",
        },
        order_updates=[order_update([]), order_update(["trade-1"])],
        trade_event={
            **confirmed_trade(),
            "maker_address": "0xmaker",
            "market_id": "market-1",
            "asset_id": "token-1",
            "shares": "10",
            "price": "0.5",
            "fee_rate_bps": "0",
            "side": "BUY",
            "execution_role": "MAKER",
            "trader_side": "MAKER",
        },
        ledger_fields={"sequence_no": 1, "ledger_id": "l", "chain_id": "c", "orderfilled_event_id": "e"},
        settlement_fields={},
        rebate_fields={},
        post_only_accepted=True,
    )
    assert row["async_trade_id_binding_ready"] is True
    assert "fill_count" not in row
