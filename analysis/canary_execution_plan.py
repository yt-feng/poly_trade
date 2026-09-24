"""Plan-only minimum reversible sizing for a future human-approved micro canary.

Pure arithmetic. No account access, credentials, signing, order submission,
cancellation, redemption, or live activation.

Public-rule references:
- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/trading/fees
- https://help.polymarket.com/en/articles/13364471-maker-rebates-program
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

D = Decimal
FEE_QUANTUM = D("0.00001")
TICK_PRECISION = {
    D("0.1"): (1, 2, 3),
    D("0.01"): (2, 2, 4),
    D("0.005"): (3, 2, 5),
    D("0.0025"): (4, 2, 6),
    D("0.001"): (3, 2, 5),
    D("0.0001"): (4, 2, 6),
}


def dec(value: Any) -> Decimal:
    try:
        out = D(str(value))
    except Exception as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return out


def tick_precision(tick_size: Any) -> tuple[int, int, int]:
    tick = dec(tick_size)
    if tick not in TICK_PRECISION:
        raise ValueError("unsupported current tick_size; refresh market rules")
    return TICK_PRECISION[tick]


def on_tick(price: Decimal, tick: Decimal) -> bool:
    return price > 0 and price < 1 and price % tick == 0


def ceil_to_tick(price: Any, tick_size: Any) -> Decimal | None:
    price, tick = dec(price), dec(tick_size)
    tick_precision(tick)
    rounded = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return rounded if D("0") < rounded < D("1") else None


def fee_usdc_conservative(shares: Any, price: Any, fee_rate: Any) -> Decimal:
    """Ceil to 5 decimals for planning; actual matched receipt remains authoritative."""
    shares, price, rate = dec(shares), dec(price), dec(fee_rate)
    if shares <= 0 or not D("0") < price < D("1") or not D("0") <= rate <= D("1"):
        raise ValueError("invalid fee inputs")
    raw = shares * rate * price * (D("1") - price)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_CEILING)


def net_buy_shares_conservative(shares: Any, price: Any, fee_rate: Any) -> Decimal:
    """BUY fee is USDC-equivalent but collected in shares on fee-enabled markets."""
    shares, price = dec(shares), dec(price)
    return shares - fee_usdc_conservative(shares, price, fee_rate) / price


def minimum_gross_buy_shares(
    min_exit_shares: Any, price: Any, fee_rate: Any, size_decimals: int = 2
) -> Decimal:
    minimum, price, rate = dec(min_exit_shares), dec(price), dec(fee_rate)
    if minimum <= 0 or size_decimals < 0:
        raise ValueError("invalid minimum/precision")
    step = D(1).scaleb(-size_decimals)
    candidate = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    for _ in range(100000):
        if net_buy_shares_conservative(candidate, price, rate) >= minimum:
            return candidate
        candidate += step
    raise RuntimeError("sizing search bound exceeded")


def build_plan(
    *,
    entry_price: Any,
    tick_size: Any,
    min_order_size: Any,
    fee_rate: Any,
    notional_cap_usd: Any = "5",
    exit_trigger: Any = "0.98",
) -> dict[str, Any]:
    price, tick = dec(entry_price), dec(tick_size)
    minimum, rate, cap = dec(min_order_size), dec(fee_rate), dec(notional_cap_usd)
    reasons: list[str] = []
    try:
        _, size_decimals, _ = tick_precision(tick)
    except ValueError:
        size_decimals = 2
        reasons.append("blocked_tick_unknown")

    if not on_tick(price, tick):
        reasons.append("blocked_entry_price_off_tick")
    if minimum <= 0:
        reasons.append("blocked_min_order_size_unknown")
    if not D("0") <= rate <= D("1"):
        reasons.append("blocked_fee_unknown")
    if cap <= 0:
        reasons.append("blocked_notional_cap")

    exit_min_price = None
    if not reasons:
        exit_min_price = ceil_to_tick(exit_trigger, tick)
        if exit_min_price is None:
            reasons.append("blocked_098_not_representable_at_current_tick")

    gross = net = fee = gross_notional = cash_cap = None
    if not reasons:
        gross = minimum_gross_buy_shares(minimum, price, rate, size_decimals)
        fee = fee_usdc_conservative(gross, price, rate)
        net = gross - fee / price
        gross_notional = (gross * price)
        # Conservative planning ceiling. Actual cash movement and fee asset must
        # be replaced by matched private receipt + balance reconciliation.
        cash_cap = gross_notional + fee
        if cash_cap > cap:
            reasons.append("blocked_minimum_reversible_size_over_notional_cap")

    return {
        "schema": 1,
        "mode": "plan_only_no_order_capability",
        "eligible_for_execution_validation_plan": not reasons,
        "block_reasons": reasons,
        "entry_price": str(price),
        "tick_size": str(tick),
        "entry_min_order_size": str(minimum),
        "fee_rate": str(rate),
        "gross_buy_shares": str(gross) if gross is not None else None,
        "conservative_buy_fee_usdc_equivalent": str(fee) if fee is not None else None,
        "conservative_net_shares": str(net) if net is not None else None,
        "gross_notional_usd": str(gross_notional) if gross_notional is not None else None,
        "conservative_cash_ceiling_usd": str(cash_cap) if cash_cap is not None else None,
        "exit_trigger_best_bid": str(dec(exit_trigger)),
        "exit_min_price_on_current_tick": str(exit_min_price) if exit_min_price is not None else None,
        "live_enabled": False,
        "orders_submitted": False,
        "required_before_any_human_approved_trade": [
            "refresh tick_size and min_order_size immediately before entry",
            "verify displayed depth at the chosen price for the full gross size",
            "after a match, replace planned shares/fees with private trade and position receipts",
            "refresh tick_size and min_order_size again before any SELL",
            "trigger 0.98 only from executable best bid or an actual match, never ask/mid",
            "reserve shares already committed to open SELL orders and handle partial fills",
            "do not reuse proceeds until trade/position/cash reconciliation is complete",
            "unresolved expiry enters settlement/redeem/cash-pending states and stops reuse",
        ],
        "note": "Planning arithmetic is deliberately conservative and is not a fill, position, or cash receipt.",
    }
