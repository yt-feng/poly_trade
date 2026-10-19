"""Deterministic feasibility bounds for a $10 account under the CLOB floors.

The frozen small-account protocol has two independent entry constraints: a
minimum number of shares and a minimum dollar notional.  The executable order
must satisfy both, so the cash bound uses their maximum rather than silently
dropping the dollar floor.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Contract:
    initial_cash: float = 10.0
    minimum_order_shares: float = 5.0
    minimum_order_notional: float = 5.0
    taker_fee_rate: float = 0.07
    fee_exponent: float = 1.0


def taker_fee(notional: float, price: float, contract: Contract = Contract()) -> float:
    if not 0.0 < price < 1.0:
        raise ValueError("PRICE_MUST_BE_BETWEEN_ZERO_AND_ONE")
    if notional < 0.0:
        raise ValueError("NOTIONAL_MUST_BE_NONNEGATIVE")
    shares = notional / price
    return shares * contract.taker_fee_rate * (price**contract.fee_exponent) * (1.0 - price)


def minimum_order_notional(price: float, contract: Contract = Contract()) -> float:
    if not 0.0 < price < 1.0:
        raise ValueError("PRICE_MUST_BE_BETWEEN_ZERO_AND_ONE")
    if contract.minimum_order_shares <= 0.0:
        raise ValueError("MINIMUM_ORDER_SHARES_MUST_BE_POSITIVE")
    if contract.minimum_order_notional < 0.0:
        raise ValueError("MINIMUM_ORDER_NOTIONAL_MUST_BE_NONNEGATIVE")
    return max(contract.minimum_order_shares * price, contract.minimum_order_notional)


def minimum_order_cash(price: float, contract: Contract = Contract()) -> float:
    notional = minimum_order_notional(price, contract)
    return notional + taker_fee(notional, price, contract)


def post_full_loss_cash(price: float, contract: Contract = Contract()) -> float:
    """Cash left after one minimum-share taker order resolves worthless."""
    return contract.initial_cash - minimum_order_cash(price, contract)


def consecutive_full_loss_capacity(price: float, contract: Contract = Contract()) -> int:
    """Return the number of full-loss minimum-share orders the ledger can fund."""
    required = minimum_order_cash(price, contract)
    cash = contract.initial_cash
    fills = 0
    while cash + 1e-12 >= required:
        cash -= required
        fills += 1
    return fills


def audit(contract: Contract = Contract()) -> dict[str, object]:
    prices = [i / 100 for i in range(1, 100)]
    post_loss = [post_full_loss_cash(price, contract) for price in prices]
    required = [minimum_order_cash(price, contract) for price in prices]
    capacities = [consecutive_full_loss_capacity(price, contract) for price in prices]
    maximum = max(post_loss)
    minimum = min(post_loss)
    return {
        "format": "capital-floor-feasibility-v3",
        "contract": asdict(contract),
        "price_grid": {"min": min(prices), "max": max(prices), "step": 0.01},
        "post_full_loss_cash": {
            "minimum": minimum,
            "maximum": maximum,
            "minimum_next_order_cash": min(required),
            "maximum_next_order_cash": max(required),
            "can_fund_second_minimum_order_for_all_grid_prices": all(
                cash + 1e-12 >= needed for cash, needed in zip(post_loss, required)
            ),
        },
        "consecutive_full_loss_capacity": {
            "minimum": min(capacities),
            "maximum": max(capacities),
            "price_at_minimum": prices[capacities.index(min(capacities))],
            "price_at_maximum": prices[capacities.index(max(capacities))],
        },
        "conclusion": "Under the documented five-share and $5 notional floors, a $10 account cannot fund a second minimum order after one full loss across the tested price grid; repeated-loss capacity is a feasibility bound, not strategy evidence.",
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = audit()
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
