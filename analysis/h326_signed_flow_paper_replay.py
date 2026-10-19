"""One predeclared signed-flow paper replay on raw BTC5m fills.

The rule uses only exchange-leg OrderFilled rows that arrived before the
entry clock.  Gamma's resolved outcome is a paper label; it is not an
owner-scoped settlement or cash receipt.  The replay uses the current crypto
taker fee formula, a five-share/$5 floor, a 600-second cash-release delay and
one chronological cash ledger.  No parameter search is performed.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from pathlib import Path
from typing import Any


INITIAL_CASH = 10.0
MIN_ORDER_SHARES = 5.0
MIN_ORDER_NOTIONAL = 5.0
FEE_RATE = 0.07
ENTRY_SECONDS = 120
FLOW_THRESHOLD = 0.25
POSITION_FRACTION = 0.60
RELEASE_DELAY_SECONDS = 600
REQUIRED_FILLS = 10
USDC_SCALE = 1_000_000.0
EXCHANGE_TAKERS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
}


def _json_string(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _epoch(value: Any) -> int:
    import datetime as dt
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace("Z", "+00:00")
    return int(dt.datetime.fromisoformat(text).timestamp())


def load_label(path: Path, start: int) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        raise ValueError("gamma_event_shape")
    event = raw[0]
    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) != 1 or not isinstance(markets[0], dict):
        raise ValueError("gamma_market_shape")
    market = markets[0]
    slug = str(market.get("slug") or event.get("slug") or "")
    expected_slug = f"btc-updown-5m-{start}"
    if slug != expected_slug:
        raise ValueError("slug_mismatch")
    outcomes = _json_string(market.get("outcomes"))
    prices = _json_string(market.get("outcomePrices"))
    tokens = _json_string(market.get("clobTokenIds"))
    if not (isinstance(outcomes, list) and isinstance(prices, list) and isinstance(tokens, list)):
        raise ValueError("label_fields_shape")
    if len(outcomes) != 2 or len(prices) != 2 or len(tokens) != 2:
        raise ValueError("binary_label_shape")
    winners = [str(outcomes[i]) for i, price in enumerate(prices) if float(price) >= 0.999]
    if len(winners) != 1 or set(str(x) for x in outcomes) != {"Up", "Down"}:
        raise ValueError("winner_not_binary")
    end_date = market.get("endDate") or event.get("endDate")
    if end_date is None or abs(_epoch(end_date) - (start + 300)) > 120:
        raise ValueError("end_time_mismatch")
    return {
        "slug": slug,
        "start": start,
        "end": start + 300,
        "token_by_outcome": {str(outcomes[i]): str(tokens[i]) for i in range(2)},
        "winner": winners[0],
        "label_source": "Gamma resolved outcomePrices; paper label only",
    }


def _fill_price_and_side(row: dict[str, Any]) -> tuple[str, float, float]:
    maker_asset = str(row["makerAssetId"])
    taker_asset = str(row["takerAssetId"])
    maker_amount = int(str(row["makerAmountFilled"])) / USDC_SCALE
    taker_amount = int(str(row["takerAmountFilled"])) / USDC_SCALE
    if maker_asset == "0" and taker_asset != "0":
        if taker_amount <= 0:
            raise ValueError("zero_shares")
        return taker_asset, maker_amount / taker_amount, taker_amount
    if taker_asset == "0" and maker_asset != "0":
        if maker_amount <= 0:
            raise ValueError("zero_shares")
        return maker_asset, taker_amount / maker_amount, maker_amount
    raise ValueError("non_binary_asset_pair")


def load_flow(path: Path, label: dict[str, Any]) -> dict[str, Any]:
    start = int(label["start"])
    token_to_outcome = {str(v): str(k) for k, v in label["token_by_outcome"].items()}
    net = {"Up": 0.0, "Down": 0.0}
    buy_prices = {"Up": [], "Down": []}
    buy_shares = {"Up": 0.0, "Down": 0.0}
    rows = 0
    valid = 0
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
            if outcome not in net or not (math.isfinite(price) and 0.0 < price < 1.0 and shares > 0):
                continue
            is_buy = str(row["makerAssetId"]) == "0"
            net[outcome] += shares if is_buy else -shares
            if is_buy:
                buy_prices[outcome].append(price)
                buy_shares[outcome] += shares
            valid += 1
    gross = abs(net["Up"]) + abs(net["Down"])
    gap = (net["Up"] - net["Down"]) / gross if gross else 0.0
    side = "Up" if gap >= FLOW_THRESHOLD else "Down" if gap <= -FLOW_THRESHOLD else None
    return {
        "start": start,
        "end": int(label["end"]),
        "slug": label["slug"],
        "winner": label["winner"],
        "rows": rows,
        "valid_exchange_rows": valid,
        "net_up": net["Up"],
        "net_down": net["Down"],
        "flow_gap": gap,
        "selected_outcome": side,
        "entry_price": max(buy_prices[side]) if side and buy_prices[side] else None,
        "selected_buy_shares": buy_shares[side] if side else 0.0,
    }


def taker_fee(shares: float, price: float) -> float:
    return shares * FEE_RATE * price * (1.0 - price)


def replay(events: list[dict[str, Any]], initial_cash: float = INITIAL_CASH) -> dict[str, Any]:
    cash = float(initial_cash)
    available_at = -float("inf")
    ledger: list[dict[str, Any]] = []
    blocked_cash = 0
    for event in sorted(events, key=lambda x: (int(x["end"]), str(x["slug"]))):
        if int(event["end"]) < available_at:
            blocked_cash += 1
            continue
        side = event.get("selected_outcome")
        price = event.get("entry_price")
        if not side or not isinstance(price, (int, float)):
            continue
        budget = cash * POSITION_FRACTION
        shares = math.floor(min(budget / price, cash / price) * 1000.0) / 1000.0
        while shares >= MIN_ORDER_SHARES:
            fee = taker_fee(shares, price)
            if shares * price + fee <= cash + 1e-12 and shares * price >= MIN_ORDER_NOTIONAL:
                break
            shares = math.floor((shares - 0.001) * 1000.0) / 1000.0
        if shares < MIN_ORDER_SHARES:
            blocked_cash += 1
            continue
        cost = shares * price
        fee = taker_fee(shares, price)
        cash -= cost + fee
        payout = shares if event["winner"] == side else 0.0
        cash += payout
        available_at = int(event["end"]) + RELEASE_DELAY_SECONDS
        ledger.append({
            "slug": event["slug"], "selected_outcome": side, "winner": event["winner"],
            "entry_price": price, "shares": shares, "fee": fee, "payout": payout,
            "cash_after_settlement": cash, "end": event["end"],
        })
    first_ten = ledger[:REQUIRED_FILLS]
    return {
        "fills": len(ledger),
        "first_ten_fills": len(first_ten),
        "first_ten_ending_cash_usd": first_ten[-1]["cash_after_settlement"] if len(first_ten) == REQUIRED_FILLS else None,
        "ending_cash_usd": cash,
        "minimum_cash_usd": min([initial_cash] + [x["cash_after_settlement"] for x in ledger]),
        "blocked_cash_events": blocked_cash,
        "ledger": ledger,
        "paper_first_ten_passed": len(first_ten) == REQUIRED_FILLS and first_ten[-1]["cash_after_settlement"] >= initial_cash,
    }


def evaluate(fills_dir: Path, labels_dir: Path, output_dir: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    label_failures: list[dict[str, Any]] = []
    for path in sorted(fills_dir.glob("*.fills.jsonl.gz")):
        start = int(path.name.split(".", 1)[0])
        try:
            label = load_label(labels_dir / f"{start}.json", start)
            events.append(load_flow(path, label))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            label_failures.append({"start": start, "reason": str(exc)})
    events.sort(key=lambda x: int(x["end"]))
    ledger = replay(events)
    split_at = max(1, int(len(events) * 0.6))
    validation_at = max(split_at + 1, int(len(events) * 0.8)) if len(events) > 1 else split_at
    splits = {
        "train": replay(events[:split_at]),
        "validation": replay(events[split_at:validation_at]),
        "test": replay(events[validation_at:]),
    }
    report = {
        "format": "h326-signed-flow-current-fee-paper-replay-v1",
        "hypothesis_id": "H326-signed-flow-current-fee-paper-replay",
        "source_scope": "raw BTC5m OrderFilled exchange legs plus Gamma resolved labels; paper only",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "position_fraction": POSITION_FRACTION,
            "entry_seconds": ENTRY_SECONDS, "flow_threshold": FLOW_THRESHOLD,
            "fee_rate": FEE_RATE, "fee_formula": "shares * fee_rate * price * (1-price)",
            "minimum_order_shares": MIN_ORDER_SHARES, "minimum_order_notional_usd": MIN_ORDER_NOTIONAL,
            "cash_release_delay_seconds": RELEASE_DELAY_SECONDS,
            "same_ledger_primary": True, "no_parameter_search": True, "no_window_concatenation": True,
        },
        "markets_observed": len(events), "label_failures": label_failures,
        "events": events, "same_ledger": ledger,
        "diagnostic_splits": {key: {k: v for k, v in value.items() if k != "ledger"} for key, value in splits.items()},
        "strategy_evaluated": True, "paper_only": True,
        "settlement_label_provenance": "Gamma outcomePrices after resolution; no owner-scoped payout or balance receipt",
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": True, "model_refit": False, "live_orders_enabled": False,
        "decision": "paper_signed_flow_replay_pending_independent_settlement_cash_join",
        "next_action": "Do not admit a Canary from this label-only replay; require owner-scoped OrderFilled, settlement, cash-release and reusable-balance checkpoints.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with (output_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["start", "slug", "rows", "valid_exchange_rows", "flow_gap", "selected_outcome", "entry_price", "winner"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows({k: row.get(k) for k in fields} for row in events)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.fills_dir, args.labels_dir, args.output_dir)
    print(json.dumps({"format": report["format"], "decision": report["decision"], "strategy_evaluated": True, "actual_fills": 0, "verified_canary_fills": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
