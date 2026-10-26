"""Explicit Polymarket market-order contract helpers.

This module is intentionally independent from the frozen H508/H301 adapter.
Polymarket's ``minimumOrderSize``/``orderMinSize`` is a USDC notional floor;
it is not a share count.  Strategy share floors, when a protocol explicitly
requires one, are kept as a separate input so the two units cannot be
silently compared.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Mapping


class ContractError(ValueError):
    """Raised when market metadata cannot support an order-contract check."""


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ContractError("observed_ts_must_be_timezone_aware")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("observed_ts_must_be_timezone_aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("observed_ts_must_be_timezone_aware")
    return parsed


@dataclass(frozen=True)
class MarketConstraints:
    """Market-level constraints with units made explicit."""

    minimum_order_size_usd: float
    tick_size: float
    itode: bool
    observed_ts: datetime


def parse_market_constraints(metadata: Mapping[str, Any]) -> MarketConstraints:
    """Parse official Gamma/CLOB fields without conflating USD and shares.

    ``minimum_order_size_usd`` is preferred for new bundles.  ``min_order_size``
    and ``orderMinSize`` are accepted aliases because these are the public
    market-detail names used by existing captures.  If more than one alias is
    present, their numeric values must agree.
    """
    if not isinstance(metadata, Mapping):
        raise ContractError("market_metadata_must_be_object")
    aliases = [
        metadata.get("minimum_order_size_usd"),
        metadata.get("min_order_size"),
        metadata.get("orderMinSize"),
    ]
    present = [_finite(value) for value in aliases if value is not None]
    if not present or any(value is None or value <= 0 for value in present):
        raise ContractError("minimum_order_size_usd_invalid")
    minimum_order_size_usd = present[0]
    if any(abs(value - minimum_order_size_usd) > 1e-12 for value in present[1:]):
        raise ContractError("minimum_order_size_alias_conflict")
    tick_size = _finite(metadata.get("tick_size"))
    if tick_size is None or tick_size <= 0:
        raise ContractError("tick_size_invalid")
    itode = metadata.get("itode")
    if type(itode) is not bool:
        raise ContractError("itode_invalid")
    observed_ts = _timestamp(metadata.get("observed_ts"))
    return MarketConstraints(
        minimum_order_size_usd=float(minimum_order_size_usd),
        tick_size=float(tick_size),
        itode=itode,
        observed_ts=observed_ts,
    )


def required_order_notional_usd(
    constraints: MarketConstraints,
    price: Any,
    *,
    strategy_minimum_shares: Any | None = None,
) -> float:
    """Return the larger of market USD floor and an optional share floor.

    The share floor is a strategy/protocol choice, not a reinterpretation of
    the market's USD ``minimumOrderSize``.  It is therefore converted at the
    proposed order price before comparison.
    """
    numeric_price = _finite(price)
    if numeric_price is None or not 0 < numeric_price < 1:
        raise ContractError("price_invalid")
    share_floor = 0.0
    if strategy_minimum_shares is not None:
        numeric_shares = _finite(strategy_minimum_shares)
        if numeric_shares is None or numeric_shares <= 0:
            raise ContractError("strategy_minimum_shares_invalid")
        share_floor = numeric_price * numeric_shares
    return max(constraints.minimum_order_size_usd, share_floor)


def price_is_on_tick(price: Any, tick_size: Any) -> bool:
    """Check a concrete submitted price using Decimal arithmetic."""
    numeric_price = _finite(price)
    numeric_tick = _finite(tick_size)
    if numeric_price is None or numeric_tick is None or numeric_tick <= 0:
        return False
    try:
        return Decimal(str(price)) % Decimal(str(tick_size)) == 0
    except (InvalidOperation, ValueError):
        return False


def validate_order_contract(
    metadata: Mapping[str, Any],
    *,
    price: Any | None = None,
    strategy_minimum_shares: Any | None = None,
    require_no_itode: bool = False,
) -> dict[str, Any]:
    """Validate metadata and optionally a concrete order contract."""
    constraints = parse_market_constraints(metadata)
    if require_no_itode and constraints.itode:
        raise ContractError("itode_not_supported_by_protocol")
    result: dict[str, Any] = {
        "minimum_order_size_usd": constraints.minimum_order_size_usd,
        "tick_size": constraints.tick_size,
        "itode": constraints.itode,
        "observed_ts": constraints.observed_ts.isoformat(),
        "unit_semantics": {
            "market_minimum": "usdc_notional",
            "strategy_minimum": "shares_optional",
        },
    }
    if price is not None:
        if not price_is_on_tick(price, constraints.tick_size):
            raise ContractError("order_price_off_tick")
        result["required_order_notional_usd"] = required_order_notional_usd(
            constraints, price, strategy_minimum_shares=strategy_minimum_shares
        )
    return result
