"""Deterministic feasibility bounds for a $10 account under the CLOB floor."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Contract:
    initial_cash: float = 10.0
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


def post_full_loss_cash(price: float, contract: Contract = Contract()) -> float:
    """Cash left after one minimum-notional taker order resolves worthless."""
    return contract.initial_cash - contract.minimum_order_notional - taker_fee(
        contract.minimum_order_notional, price, contract
    )


def audit(contract: Contract = Contract()) -> dict[str, object]:
    # The bound is analytic for exponent 1: fee = N*r*(1-p), so every p<1
    # leaves strictly less than N cash after a full loss.
    prices = [i / 100 for i in range(1, 100)]
    post_loss = [post_full_loss_cash(price, contract) for price in prices]
    maximum = max(post_loss)
    minimum = min(post_loss)
    return {
        "format": "capital-floor-feasibility-v1",
        "contract": asdict(contract),
        "price_grid": {"min": min(prices), "max": max(prices), "step": 0.01},
        "post_full_loss_cash": {
            "minimum": minimum,
            "maximum": maximum,
            "strictly_below_next_minimum_order_for_all_grid_prices": maximum < contract.minimum_order_notional,
        },
        "conclusion": "A $10 account using a $5 taker minimum cannot fund a second minimum order after any full loss at a price strictly below $1; maker-only execution or more starting cash is required for repeated canary attempts.",
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
