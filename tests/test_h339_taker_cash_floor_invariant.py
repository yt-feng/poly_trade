from decimal import Decimal

from analysis.h339_taker_cash_floor_invariant import analyze


def test_no_crypto_taker_price_preserves_five_dollars_after_minimum_entry():
    result = analyze()
    assert result["price_grid_count"] == 99
    assert result["taker_rows_passing_cash_floor"] == 0
    assert Decimal(result["maximum_taker_post_entry_free_cash_usd"]) < Decimal("5")
    assert result["decision"] == "taker_contract_cannot_preserve_minimum_cash_after_first_entry_at_10_usd"


def test_zero_fee_maker_entry_reaches_the_floor_but_is_not_a_fill_claim():
    result = analyze()
    assert result["maker_post_entry_free_cash_usd"] == "5.00000"
    assert result["maker_row_passes_cash_floor"] is True
    assert result["actual_fills"] == 0
