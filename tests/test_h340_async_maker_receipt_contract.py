from analysis.h340_async_maker_receipt_contract import audit


def test_async_trade_id_is_preserved_without_counting_a_fill():
    result = audit()
    assert result["async_order_trade_ids_preserved"] is True
    assert result["async_order_emits_no_fill"] is True


def test_confirmed_maker_trade_requires_transaction_reference():
    result = audit()
    assert result["confirmed_maker_fill_emitted"] is True
    assert result["missing_transaction_rejected"] is True
    assert result["external_owner_settlement_cash_still_required"] is True
