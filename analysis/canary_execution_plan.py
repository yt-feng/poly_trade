"""Plan-only minimum reversible sizing for a future human-approved micro canary.

Pure arithmetic. No account access, credentials, signing, order submission,
cancellation, redemption, or live activation.

The default fee treatment follows the current Polymarket fee documentation:
taker fees are calculated in USDC at match time. A separate share-deduction
scenario is retained only as an explicit research sensitivity so historical
studies using that convention are not silently mixed with the official default.

Public-rule references:
- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/trading/fees
- https://help.polymarket.com/en/articles/13364471-maker-rebates-program
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Any

D = Decimal
FEE_QUANTUM = D("0.00001")
OFFICIAL_CASH_FEE = "cash_usdc_official"
SHARE_FEE_SENSITIVITY = "shares_sensitivity"
FEE_ASSET_SCENARIOS = (OFFICIAL_CASH_FEE, SHARE_FEE_SENSITIVITY)
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
    """Plan the matched taker fee in USDC, rounded against the trader to 5 decimals."""
    shares, price, rate = dec(shares), dec(price), dec(fee_rate)
    if shares <= 0 or not D("0") < price < D("1") or not D("0") <= rate <= D("1"):
        raise ValueError("invalid fee inputs")
    raw = shares * rate * price * (D("1") - price)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_CEILING)


def net_buy_shares_conservative(
    shares: Any,
    price: Any,
    fee_rate: Any,
    fee_asset_scenario: str = OFFICIAL_CASH_FEE,
) -> Decimal:
    """Return shares available for exit under an explicit fee-asset scenario.

    Current official treatment: the taker fee is a USDC cost and does not reduce
    the matched share quantity. `shares_sensitivity` preserves the older
    share-deduction convention solely for sensitivity analysis.
    """
    shares, price = dec(shares), dec(price)
    if fee_asset_scenario == OFFICIAL_CASH_FEE:
        return shares
    if fee_asset_scenario == SHARE_FEE_SENSITIVITY:
        return shares - fee_usdc_conservative(shares, price, fee_rate) / price
    raise ValueError("unknown fee_asset_scenario")


def minimum_gross_buy_shares(
    min_exit_shares: Any,
    price: Any,
    fee_rate: Any,
    size_decimals: int = 2,
    fee_asset_scenario: str = OFFICIAL_CASH_FEE,
) -> Decimal:
    minimum, price, rate = dec(min_exit_shares), dec(price), dec(fee_rate)
    if minimum <= 0 or size_decimals < 0:
        raise ValueError("invalid minimum/precision")
    if fee_asset_scenario not in FEE_ASSET_SCENARIOS:
        raise ValueError("unknown fee_asset_scenario")
    step = D(1).scaleb(-size_decimals)
    candidate = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    for _ in range(100000):
        if net_buy_shares_conservative(candidate, price, rate, fee_asset_scenario) >= minimum:
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
    fee_asset_scenario: str = OFFICIAL_CASH_FEE,
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
    if fee_asset_scenario not in FEE_ASSET_SCENARIOS:
        reasons.append("blocked_fee_asset_scenario_unknown")

    exit_min_price = None
    if not reasons:
        exit_min_price = ceil_to_tick(exit_trigger, tick)
        if exit_min_price is None:
            reasons.append("blocked_098_not_representable_at_current_tick")

    gross = net = fee = gross_notional = all_in = cash_spend = None
    if not reasons:
        gross = minimum_gross_buy_shares(
            minimum, price, rate, size_decimals, fee_asset_scenario
        )
        fee = fee_usdc_conservative(gross, price, rate)
        net = net_buy_shares_conservative(gross, price, rate, fee_asset_scenario)
        gross_notional = gross * price
        # Risk cap is applied to a common USD-equivalent basis under either
        # scenario so fee-asset assumptions never loosen the canary budget.
        all_in = gross_notional + fee
        # Under the official scenario this is the planned USDC cash outflow.
        # The sensitivity scenario is deliberately not represented as an
        # official cash debit; its `all_in` value is only a cost-equivalent.
        cash_spend = all_in if fee_asset_scenario == OFFICIAL_CASH_FEE else gross_notional
        if all_in > cap:
            reasons.append("blocked_minimum_reversible_size_over_notional_cap")

    return {
        "schema": 2,
        "mode": "plan_only_no_order_capability",
        "eligible_for_execution_validation_plan": not reasons,
        "block_reasons": reasons,
        "entry_price": str(price),
        "tick_size": str(tick),
        "entry_min_order_size": str(minimum),
        "fee_rate": str(rate),
        "fee_asset_scenario": fee_asset_scenario,
        "fee_scenario_status": (
            "current_official_default"
            if fee_asset_scenario == OFFICIAL_CASH_FEE
            else "research_sensitivity_not_current_official_default"
        ),
        "gross_buy_shares": str(gross) if gross is not None else None,
        "planned_taker_fee_usdc": str(fee) if fee is not None else None,
        "conservative_net_shares": str(net) if net is not None else None,
        "gross_notional_usd": str(gross_notional) if gross_notional is not None else None,
        "all_in_cost_usd_equivalent": str(all_in) if all_in is not None else None,
        "planned_cash_spend_usd": str(cash_spend) if cash_spend is not None else None,
        "exit_trigger_best_bid": str(dec(exit_trigger)),
        "exit_min_price_on_current_tick": str(exit_min_price) if exit_min_price is not None else None,
        "live_enabled": False,
        "orders_submitted": False,
        "required_before_any_human_approved_trade": [
            "refresh tick_size and min_order_size immediately before entry",
            "verify displayed depth at the chosen price for the full gross size",
            "after a match, replace planned shares, fee amount and fee asset with private trade and position receipts",
            "refresh tick_size and min_order_size again before any SELL",
            "trigger 0.98 only from executable best bid or an actual match, never ask/mid",
            "reserve shares already committed to open SELL orders and handle partial fills",
            "do not reuse proceeds until trade/position/cash reconciliation is complete",
            "unresolved expiry enters settlement/redeem/cash-pending states and stops reuse",
        ],
        "note": "Planning arithmetic is not a fill, position, fee receipt, or cash receipt; private matched receipts remain authoritative.",
    }
