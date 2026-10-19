from __future__ import annotations

import pytest

from analysis.capital_floor_feasibility import (
    Contract,
    audit,
    consecutive_full_loss_capacity,
    minimum_order_cash,
    post_full_loss_cash,
    taker_fee,
)


def test_crypto_taker_fee_matches_minimum_notional_formula() -> None:
    contract = Contract()
    assert taker_fee(2.5, 0.5, contract) == pytest.approx(0.0875)


def test_full_loss_leaves_less_than_next_minimum_order() -> None:
    result = audit()
    assert result["post_full_loss_cash"]["can_fund_second_minimum_order_for_all_grid_prices"] is False
    assert post_full_loss_cash(0.5) == pytest.approx(4.825)


def test_share_floor_cash_requirement_scales_with_price() -> None:
    assert minimum_order_cash(0.5) == pytest.approx(5.175)
    assert minimum_order_cash(0.2) == pytest.approx(5.28)


def test_repeated_full_loss_capacity_is_price_dependent() -> None:
    assert consecutive_full_loss_capacity(0.5) == 1
    assert consecutive_full_loss_capacity(0.99) == 1


def test_conditional_share_floor_can_be_a_separate_explicit_counterfactual() -> None:
    share_only = Contract(minimum_order_notional=0.0)
    assert minimum_order_cash(0.5, share_only) == pytest.approx(2.5875)
    assert post_full_loss_cash(0.5, share_only) == pytest.approx(7.4125)

