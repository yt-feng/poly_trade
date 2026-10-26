from __future__ import annotations

import pytest

from analysis.order_floor_contract import required_order_cost


def test_dual_order_floor_keeps_usd_and_share_units_distinct() -> None:
    assert required_order_cost(0.40, minimum_order_usd=5.0, minimum_order_shares=5.0) == pytest.approx(5.0)
    assert required_order_cost(1.50, minimum_order_usd=1.0, minimum_order_shares=5.0) == pytest.approx(7.5)


def test_invalid_fill_price_fails_closed() -> None:
    with pytest.raises(ValueError, match="fill_price_must_be_positive"):
        required_order_cost(0.0)
