"""Predeclared gross buy-volume pressure paper replay for BTC5m.

The statistic differs from H326's signed flow and H327's maker concentration:
it compares total pre-entry buy notional by outcome, without cancelling buys
against sells and without selecting a wallet.  All execution and cash gates
remain frozen.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from pathlib import Path
from typing import Any

try:
    from analysis.h326_signed_flow_paper_replay import (
        ENTRY_SECONDS, EXCHANGE_TAKERS, INITIAL_CASH, MIN_ORDER_NOTIONAL,
        MIN_ORDER_SHARES, POSITION_FRACTION, RELEASE_DELAY_SECONDS,
        _fill_price_and_side, load_label, replay,
    )
except ModuleNotFoundError:
    from h326_signed_flow_paper_replay import (
        ENTRY_SECONDS, EXCHANGE_TAKERS, INITIAL_CASH, MIN_ORDER_NOTIONAL,
        MIN_ORDER_SHARES, POSITION_FRACTION, RELEASE_DELAY_SECONDS,
        _fill_price_and_side, load_label, replay,
    )


BUY_SHARE_THRESHOLD = 0.65
MIN_GROSS_BUY_NOTIONAL_USD = 5.0


def choose_gross_pressure(
    buy_notional: dict[str, float], buy_prices: dict[str, list[float]],
) -> tuple[str | None, float, float | None]:
    total = sum(max(0.0, value) for value in buy_notional.values())
    if total < MIN_GROSS_BUY_NOTIONAL_USD:
        return None, 0.0, None
    side = max(("Up", "Down"), key=lambda name: buy_notional[name])
    share = buy_notional[side] / total
    if share < BUY_SHARE_THRESHOLD or not buy_prices[side]:
        return None, share, None
    return side, share, max(buy_prices[side])


def load_pressure(path: Path, label: dict[str, Any]) -> dict[str, Any]:
    start = int(label["start"])
    token_to_outcome = {str(v): str(k) for k, v in label["token_by_outcome"].items()}
    buy_notional = {"Up": 0.0, "Down": 0.0}
    buy_prices = {"Up": [], "Down": []}
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
            if outcome not in buy_notional or not (math.isfinite(price) and 0.0 < price < 1.0 and shares > 0):
                continue
            if str(row["makerAssetId"]) == "0":
                buy_notional[outcome] += price * shares
                buy_prices[outcome].append(price)
            valid += 1
    side, share, price = choose_gross_pressure(buy_notional, buy_prices)
    return {
        "start": start, "end": int(label["end"]), "slug": label["slug"], "winner": label["winner"],
        "rows": rows, "valid_exchange_rows": valid, "buy_notional_up": buy_notional["Up"],
        "buy_notional_down": buy_notional["Down"], "selected_outcome": side,
        "gross_buy_share": share, "entry_price": price,
    }


def evaluate(fills_dir: Path, labels_dir: Path, output_dir: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in sorted(fills_dir.glob("*.fills.jsonl.gz")):
        start = int(path.name.split(".", 1)[0])
        try:
            label = load_label(labels_dir / f"{start}.json", start)
            events.append(load_pressure(path, label))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures.append({"start": start, "reason": str(exc)})
    events.sort(key=lambda x: int(x["end"]))
    ledger = replay(events, initial_cash=INITIAL_CASH)
    split_at = max(1, int(len(events) * 0.6))
    validation_at = max(split_at + 1, int(len(events) * 0.8)) if len(events) > 1 else split_at
    splits = {"train": replay(events[:split_at]), "validation": replay(events[split_at:validation_at]), "test": replay(events[validation_at:])}
    report = {
        "format": "h328-gross-buy-pressure-paper-replay-v1",
        "hypothesis_id": "H328-gross-buy-volume-pressure-paper-replay",
        "source_scope": "raw BTC5m OrderFilled exchange legs plus Gamma resolved labels; paper only",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "position_fraction": POSITION_FRACTION,
            "entry_seconds": ENTRY_SECONDS, "gross_buy_share_threshold": BUY_SHARE_THRESHOLD,
            "minimum_gross_buy_notional_usd": MIN_GROSS_BUY_NOTIONAL_USD,
            "fee_formula": "shares * 0.07 * price * (1-price)", "minimum_order_shares": MIN_ORDER_SHARES,
            "minimum_order_notional_usd": MIN_ORDER_NOTIONAL, "cash_release_delay_seconds": RELEASE_DELAY_SECONDS,
            "same_ledger_primary": True, "no_parameter_search": True, "no_window_concatenation": True,
        },
        "markets_observed": len(events), "label_failures": failures, "events": events, "same_ledger": ledger,
        "diagnostic_splits": {key: {k: v for k, v in value.items() if k != "ledger"} for key, value in splits.items()},
        "strategy_evaluated": True, "paper_only": True,
        "settlement_label_provenance": "Gamma outcomePrices after resolution; no owner-scoped payout or balance receipt",
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": True, "model_refit": False, "live_orders_enabled": False,
        "decision": "paper_gross_buy_pressure_replay_pending_independent_settlement_cash_join",
        "next_action": "Do not admit a Canary from gross buy pressure; require owner-scoped settlement, cash release and reusable balance checkpoints.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with (output_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["start", "slug", "rows", "valid_exchange_rows", "buy_notional_up", "buy_notional_down", "selected_outcome", "gross_buy_share", "entry_price", "winner"]
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
