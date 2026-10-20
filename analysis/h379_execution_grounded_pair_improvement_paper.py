"""Pre-registered execution-grounded BTC5m paper replay.

The candidate uses only exchange-taker OrderFilled rows observed before the
120-second entry clock.  It requires agreement between signed flow and gross
buy pressure, then requires a complementary-side price improvement signal.
The replay is paper-only: Gamma outcome labels are not settlement receipts and
no row can count as an authenticated Canary fill.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import json
import math
from pathlib import Path
from typing import Any

try:
    from analysis.h326_signed_flow_paper_replay import (
        EXCHANGE_TAKERS,
        _fill_price_and_side,
        load_label,
    )
except ModuleNotFoundError:
    from h326_signed_flow_paper_replay import (
        EXCHANGE_TAKERS,
        _fill_price_and_side,
        load_label,
    )


INITIAL_CASH = 10.0
MIN_ORDER_SHARES = 5.0
MIN_ORDER_NOTIONAL = 5.0
FEE_RATE = 0.07
ENTRY_SECONDS = 120
SIGNED_FLOW_THRESHOLD = 0.25
GROSS_BUY_SHARE_THRESHOLD = 0.65
PAIR_PRICE_IMPROVEMENT_THRESHOLD = 0.03
POSITION_FRACTION = 0.60
RELEASE_DELAY_SECONDS = 600
REQUIRED_FILLS = 10
USDC_SCALE = 1_000_000.0


def taker_fee(shares: float, price: float) -> float:
    return shares * FEE_RATE * price * (1.0 - price)


def _weighted_price(prices: list[tuple[float, float]]) -> float | None:
    total_shares = sum(shares for _price, shares in prices)
    if total_shares <= 0:
        return None
    return sum(price * shares for price, shares in prices) / total_shares


def load_execution_edge(path: Path, label: dict[str, Any]) -> dict[str, Any]:
    """Build one causal market feature row from pre-entry OrderFilled events."""
    start = int(label["start"])
    token_to_outcome = {str(v): str(k) for k, v in label["token_by_outcome"].items()}
    signed = {"Up": 0.0, "Down": 0.0}
    buy_notional = {"Up": 0.0, "Down": 0.0}
    buy_prices: dict[str, list[tuple[float, float]]] = {"Up": [], "Down": []}
    rows = valid = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            rows += 1
            row = json.loads(line)
            if str(row.get("taker", "")).lower() not in EXCHANGE_TAKERS:
                continue
            timestamp = int(str(row["timestamp"]))
            if not start <= timestamp < start + ENTRY_SECONDS:
                continue
            token, price, shares = _fill_price_and_side(row)
            outcome = token_to_outcome.get(token)
            if outcome not in signed or not (math.isfinite(price) and 0.0 < price < 1.0 and shares > 0):
                continue
            is_buy = str(row["makerAssetId"]) == "0"
            signed[outcome] += shares if is_buy else -shares
            if is_buy:
                buy_notional[outcome] += price * shares
                buy_prices[outcome].append((price, shares))
            valid += 1

    gross_signed = abs(signed["Up"]) + abs(signed["Down"])
    signed_gap = (signed["Up"] - signed["Down"]) / gross_signed if gross_signed else 0.0
    gross_buys = buy_notional["Up"] + buy_notional["Down"]
    gross_buy_share = max(buy_notional.values()) / gross_buys if gross_buys else 0.0
    weighted = {outcome: _weighted_price(buy_prices[outcome]) for outcome in ("Up", "Down")}
    pair_improvement = (
        1.0 - weighted["Up"] - weighted["Down"]
        if weighted["Up"] is not None and weighted["Down"] is not None
        else None
    )
    selected = None
    if abs(signed_gap) >= SIGNED_FLOW_THRESHOLD and gross_buy_share >= GROSS_BUY_SHARE_THRESHOLD:
        selected = "Up" if signed_gap > 0 else "Down"
    if selected is not None and (
        pair_improvement is None or pair_improvement < PAIR_PRICE_IMPROVEMENT_THRESHOLD
    ):
        selected = None
    entry_prices = buy_prices.get(selected or "", [])
    entry_price = max((price for price, _shares in entry_prices), default=None)
    return {
        "start": start,
        "end": int(label["end"]),
        "slug": label["slug"],
        "winner": label["winner"],
        "rows": rows,
        "valid_exchange_rows": valid,
        "signed_gap": signed_gap,
        "gross_buy_share": gross_buy_share,
        "weighted_buy_price_up": weighted["Up"],
        "weighted_buy_price_down": weighted["Down"],
        "pair_price_improvement": pair_improvement,
        "selected_outcome": selected,
        "entry_price": entry_price,
    }


def _shares_for_cash(cash: float, price: float) -> float:
    shares = math.floor(min(cash * POSITION_FRACTION / price, cash / price) * 1000.0) / 1000.0
    while shares >= MIN_ORDER_SHARES:
        cost = shares * price
        if cost >= MIN_ORDER_NOTIONAL and cost + taker_fee(shares, price) <= cash + 1e-12:
            return shares
        shares = math.floor((shares - 0.001) * 1000.0) / 1000.0
    return 0.0


def replay(events: list[dict[str, Any]], initial_cash: float = INITIAL_CASH) -> dict[str, Any]:
    """Replay one chronological ledger with explicit cash release."""
    cash = float(initial_cash)
    pending: list[tuple[int, float]] = []
    ledger: list[dict[str, Any]] = []
    blocked_cash = 0
    minimum_cash = cash
    for event in sorted(events, key=lambda row: (int(row["end"]), str(row["slug"]))):
        now = int(event["end"])
        while pending and pending[0][0] <= now:
            _release_ts, payout = heapq.heappop(pending)
            cash += payout
        side = event.get("selected_outcome")
        price = event.get("entry_price")
        if not side or not isinstance(price, (int, float)):
            continue
        shares = _shares_for_cash(cash, float(price))
        if shares < MIN_ORDER_SHARES:
            blocked_cash += 1
            continue
        cost = shares * float(price)
        fee = taker_fee(shares, float(price))
        reserved = cost + fee
        if reserved > cash + 1e-12:
            blocked_cash += 1
            continue
        cash -= reserved
        payout = shares if event["winner"] == side else 0.0
        minimum_cash = min(minimum_cash, cash)
        heapq.heappush(pending, (now + RELEASE_DELAY_SECONDS, payout))
        ledger.append({
            "slug": event["slug"],
            "selected_outcome": side,
            "winner": event["winner"],
            "entry_price": float(price),
            "shares": shares,
            "fee": fee,
            "reserved": reserved,
            "payout": payout,
            "entry_end": now,
            "cash_after_entry": cash,
        })
    while pending:
        _release_ts, payout = heapq.heappop(pending)
        cash += payout
    first_ten = ledger[:REQUIRED_FILLS]
    first_ten_cash = (
        initial_cash
        - sum(float(row["reserved"]) for row in first_ten)
        + sum(float(row["payout"]) for row in first_ten)
        if len(first_ten) == REQUIRED_FILLS
        else None
    )
    return {
        "fills": len(ledger),
        "first_ten_fills": len(first_ten),
        "first_ten_ending_cash_usd": first_ten_cash,
        "ending_cash_usd": cash,
        "minimum_cash_usd": minimum_cash,
        "blocked_cash_events": blocked_cash,
        "paper_first_ten_passed": first_ten_cash is not None and first_ten_cash >= initial_cash,
        "ledger": ledger,
    }


def evaluate(fills_dir: Path, labels_dir: Path, output_dir: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in sorted(fills_dir.glob("*.fills.jsonl.gz")):
        start = int(path.name.split(".", 1)[0])
        try:
            events.append(load_execution_edge(path, load_label(labels_dir / f"{start}.json", start)))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures.append({"start": start, "reason": str(exc)})
    events.sort(key=lambda row: int(row["end"]))
    split_at = max(1, int(len(events) * 0.6))
    validation_at = max(split_at + 1, int(len(events) * 0.8)) if len(events) > 1 else split_at
    report = {
        "format": "h379-execution-grounded-pair-improvement-paper-v1",
        "hypothesis_id": "H379-execution-grounded-pair-improvement",
        "source_scope": "fixed public raw v1 OrderFilled BTC5m windows plus Gamma labels; paper only",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "position_fraction": POSITION_FRACTION,
            "entry_seconds": ENTRY_SECONDS,
            "signed_flow_threshold": SIGNED_FLOW_THRESHOLD,
            "gross_buy_share_threshold": GROSS_BUY_SHARE_THRESHOLD,
            "pair_price_improvement_threshold": PAIR_PRICE_IMPROVEMENT_THRESHOLD,
            "fee_rate": FEE_RATE,
            "minimum_order_shares": MIN_ORDER_SHARES,
            "minimum_order_notional_usd": MIN_ORDER_NOTIONAL,
            "cash_release_delay_seconds": RELEASE_DELAY_SECONDS,
            "same_evolving_cash_ledger": True,
            "no_parameter_search": True,
            "no_window_concatenation": True,
        },
        "markets_observed": len(events),
        "label_failures": failures,
        "diagnostic_splits": {
            "train": replay(events[:split_at]),
            "validation": replay(events[split_at:validation_at]),
            "test": replay(events[validation_at:]),
        },
        "events": events,
        "strategy_evaluated": True,
        "paper_only": True,
        "settlement_label_provenance": "Gamma outcomePrices after resolution; no owner-scoped payout or balance receipt",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": True,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "paper_execution_edge_pending_independent_settlement_cash_join",
        "next_action": "Do not admit Canary from this label-only replay; require owner-scoped OrderFilled, settlement, cash-release and reusable-balance checkpoints.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["start", "slug", "valid_exchange_rows", "signed_gap", "gross_buy_share", "pair_price_improvement", "selected_outcome", "entry_price", "winner"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in events)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.fills_dir, args.labels_dir, args.output_dir)
    print(json.dumps({"format": report["format"], "decision": report["decision"], "actual_fills": 0, "verified_canary_fills": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
