"""Prove the first-entry cash-floor invariant for the frozen BTC5m contract.

This is a deterministic contract check, not a market-data or strategy run.  It
enumerates the documented crypto price grid, applies the documented taker fee
formula and rounding, and checks whether a $10 account can place the minimum
$5 order while retaining another $5 of free cash immediately after entry.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
from typing import Any


INITIAL_CASH = Decimal("10")
MIN_ORDER = Decimal("5")
MIN_FREE_CASH = Decimal("5")
TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0")
PRICE_MIN = Decimal("0.01")
PRICE_MAX = Decimal("0.99")
PRICE_TICK = Decimal("0.01")
FEE_QUANTUM = Decimal("0.00001")


def _fee(price: Decimal, rate: Decimal) -> Decimal:
    shares = MIN_ORDER / price
    raw = shares * rate * price * (Decimal("1") - price)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def analyze() -> dict[str, Any]:
    prices: list[Decimal] = []
    current = PRICE_MIN
    while current <= PRICE_MAX:
        prices.append(current)
        current += PRICE_TICK
    taker_rows = []
    for price in prices:
        fee = _fee(price, TAKER_RATE)
        taker_rows.append({
            "price": str(price),
            "fee_usd": str(fee),
            "post_entry_free_cash_usd": str(INITIAL_CASH - MIN_ORDER - fee),
            "passes_cash_floor": INITIAL_CASH - MIN_ORDER - fee >= MIN_FREE_CASH,
        })
    best = max(taker_rows, key=lambda row: Decimal(row["post_entry_free_cash_usd"]))
    worst = min(taker_rows, key=lambda row: Decimal(row["post_entry_free_cash_usd"]))
    min_fee = min(Decimal(row["fee_usd"]) for row in taker_rows)
    maker_post = INITIAL_CASH - MIN_ORDER - _fee(PRICE_MIN, MAKER_RATE)
    return {
        "format": "h339-taker-cash-floor-invariant-v1",
        "hypothesis_id": "H339-taker-cash-floor-invariant",
        "source_scope": "deterministic frozen execution contract plus current official Polymarket documentation; no market data, refit, or order submission",
        "protocol": {
            "initial_cash_usd": str(INITIAL_CASH),
            "minimum_order_notional_usd": str(MIN_ORDER),
            "minimum_free_cash_after_entry_usd": str(MIN_FREE_CASH),
            "price_grid": [str(PRICE_MIN), str(PRICE_MAX), str(PRICE_TICK)],
            "fee_rounding_decimal_places": 5,
        },
        "official_sources": {
            "fees_url": "https://docs.polymarket.com/trading/fees",
            "market_details_url": "https://docs.polymarket.com/market-data/market-details#trading-constraints",
            "documented_crypto_taker_rate": str(TAKER_RATE),
            "documented_crypto_maker_rate": str(MAKER_RATE),
            "documented_minimum_order_size_usdc": str(MIN_ORDER),
        },
        "price_grid_count": len(prices),
        "minimum_taker_fee_usd": str(min_fee),
        "best_taker_price": best["price"],
        "maximum_taker_post_entry_free_cash_usd": best["post_entry_free_cash_usd"],
        "worst_taker_price": worst["price"],
        "minimum_taker_post_entry_free_cash_usd": worst["post_entry_free_cash_usd"],
        "taker_rows_passing_cash_floor": sum(bool(row["passes_cash_floor"]) for row in taker_rows),
        "maker_post_entry_free_cash_usd": str(maker_post),
        "maker_row_passes_cash_floor": maker_post >= MIN_FREE_CASH,
        "decision": "taker_contract_cannot_preserve_minimum_cash_after_first_entry_at_10_usd",
        "root_cause": "For every documented crypto taker price from $0.01 through $0.99, the minimum $5 order incurs a positive rounded fee. Starting from $10 therefore leaves less than $5 immediately after the first entry. A zero-fee maker entry is the only fee-side escape, but it needs authenticated queue, fill, settlement and reusable-cash evidence before it can enter Canary research.",
        "strategy_evaluated": False,
        "paper_only": True,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "next_action": "Do not tune the taker model for this gate. Register only a maker-evidence experiment with owner-scoped queue/fill/settlement/reusable-cash receipts; keep live buying paused.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = analyze()
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: result[key] for key in (
        "minimum_taker_fee_usd", "maximum_taker_post_entry_free_cash_usd",
        "taker_rows_passing_cash_floor", "maker_post_entry_free_cash_usd",
        "decision",
    )}, sort_keys=True))


if __name__ == "__main__":
    main()
