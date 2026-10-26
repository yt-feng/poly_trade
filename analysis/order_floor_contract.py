"""Unit-explicit minimum order cost helpers for paper execution replays."""
from __future__ import annotations

import math

DEFAULT_MINIMUM_ORDER_USD = 5.0
DEFAULT_MINIMUM_ORDER_SHARES = 5.0


def required_order_cost(
    fill_price: float,
    *,
    minimum_order_usd: float = DEFAULT_MINIMUM_ORDER_USD,
    minimum_order_shares: float = DEFAULT_MINIMUM_ORDER_SHARES,
) -> float:
    """Return max(USDC notional floor, share floor converted at fill price)."""
    price = float(fill_price)
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError("fill_price_must_be_positive")
    usd_floor = float(minimum_order_usd)
    shares_floor = float(minimum_order_shares)
    if not math.isfinite(usd_floor) or not math.isfinite(shares_floor):
        raise ValueError("minimum_order_floor_must_be_finite")
    if usd_floor < 0.0 or shares_floor < 0.0:
        raise ValueError("minimum_order_floor_must_be_nonnegative")
    return max(usd_floor, shares_floor * price)
