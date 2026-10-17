from __future__ import annotations

import pytest

from analysis.capital_floor_feasibility import Contract, audit, post_full_loss_cash, taker_fee


def test_crypto_taker_fee_matches_minimum_notional_formula() -> None:
    contract = Contract()
    assert taker_fee(5.0, 0.5, contract) == pytest.approx(0.175)


def test_full_loss_leaves_less_than_next_minimum_order() -> None:
    result = audit()
    assert result["post_full_loss_cash"]["maximum"] < 5.0
    assert result["post_full_loss_cash"]["strictly_below_next_minimum_order_for_all_grid_prices"] is True
    assert post_full_loss_cash(0.5) == pytest.approx(4.825)

