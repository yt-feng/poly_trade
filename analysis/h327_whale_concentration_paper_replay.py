"""Predeclared large-maker concentration paper replay for BTC5m.

This is a distinct statistic from H326's aggregate signed-flow gap.  It uses
only the first 120 seconds of raw exchange-leg OrderFilled rows and selects a
side when one maker contributes at least 60% of the absolute signed notional
in that market and the selected side has positive net flow.  Maker addresses
are used only inside the sealed computation; no wallet sample is published.
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
        REQUIRED_FILLS, _fill_price_and_side, load_label, replay,
    )
except ModuleNotFoundError:  # direct `python analysis/...py` invocation
    from h326_signed_flow_paper_replay import (
        ENTRY_SECONDS, EXCHANGE_TAKERS, INITIAL_CASH, MIN_ORDER_NOTIONAL,
        MIN_ORDER_SHARES, POSITION_FRACTION, RELEASE_DELAY_SECONDS,
        REQUIRED_FILLS, _fill_price_and_side, load_label, replay,
    )


CONCENTRATION_THRESHOLD = 0.60
MIN_SIGNED_NOTIONAL_USD = 5.0


def choose_concentrated_side(
    net: dict[str, float], signed_by_maker: dict[str, dict[str, float]], buy_prices: dict[str, list[float]],
) -> tuple[str | None, float, float | None]:
    total_abs = sum(abs(value) for side in signed_by_maker.values() for value in side.values())
    if total_abs < MIN_SIGNED_NOTIONAL_USD:
        return None, 0.0, None
    side = max(("Up", "Down"), key=lambda name: net[name])
    if net[side] <= 0 or not buy_prices[side]:
        return None, 0.0, None
    concentration = max((abs(value) for value in signed_by_maker[side].values()), default=0.0) / total_abs
    if concentration < CONCENTRATION_THRESHOLD:
        return None, concentration, None
    return side, concentration, max(buy_prices[side])


def load_concentration(path: Path, label: dict[str, Any]) -> dict[str, Any]:
    start = int(label["start"])
    token_to_outcome = {str(v): str(k) for k, v in label["token_by_outcome"].items()}
    net = {"Up": 0.0, "Down": 0.0}
    signed_by_maker = {"Up": {}, "Down": {}}
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
            if outcome not in net or not (math.isfinite(price) and 0.0 < price < 1.0 and shares > 0):
                continue
            is_buy = str(row["makerAssetId"]) == "0"
            signed_notional = price * shares * (1.0 if is_buy else -1.0)
            maker = str(row.get("maker", "")).lower()
            net[outcome] += signed_notional
            signed_by_maker[outcome][maker] = signed_by_maker[outcome].get(maker, 0.0) + signed_notional
            if is_buy:
                buy_prices[outcome].append(price)
            valid += 1
    side, concentration, price = choose_concentrated_side(net, signed_by_maker, buy_prices)
    return {
        "start": start, "end": int(label["end"]), "slug": label["slug"], "winner": label["winner"],
        "rows": rows, "valid_exchange_rows": valid, "net_up": net["Up"], "net_down": net["Down"],
        "selected_outcome": side, "top_maker_concentration": concentration, "entry_price": price,
    }


def evaluate(fills_dir: Path, labels_dir: Path, output_dir: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in sorted(fills_dir.glob("*.fills.jsonl.gz")):
        start = int(path.name.split(".", 1)[0])
        try:
            label = load_label(labels_dir / f"{start}.json", start)
            events.append(load_concentration(path, label))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures.append({"start": start, "reason": str(exc)})
    events.sort(key=lambda x: int(x["end"]))
    ledger = replay(events, initial_cash=INITIAL_CASH)
    split_at = max(1, int(len(events) * 0.6))
    validation_at = max(split_at + 1, int(len(events) * 0.8)) if len(events) > 1 else split_at
    splits = {"train": replay(events[:split_at]), "validation": replay(events[split_at:validation_at]), "test": replay(events[validation_at:])}
    report = {
        "format": "h327-large-maker-concentration-paper-replay-v1",
        "hypothesis_id": "H327-large-maker-concentration-paper-replay",
        "source_scope": "raw BTC5m OrderFilled exchange legs plus Gamma resolved labels; paper only",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "position_fraction": POSITION_FRACTION,
            "entry_seconds": ENTRY_SECONDS, "concentration_threshold": CONCENTRATION_THRESHOLD,
            "minimum_signed_notional_usd": MIN_SIGNED_NOTIONAL_USD,
            "fee_formula": "shares * 0.07 * price * (1-price)", "minimum_order_shares": MIN_ORDER_SHARES,
            "minimum_order_notional_usd": MIN_ORDER_NOTIONAL, "cash_release_delay_seconds": RELEASE_DELAY_SECONDS,
            "same_ledger_primary": True, "no_parameter_search": True, "no_window_concatenation": True,
        },
        "markets_observed": len(events), "label_failures": failures, "events": events,
        "same_ledger": ledger,
        "diagnostic_splits": {key: {k: v for k, v in value.items() if k != "ledger"} for key, value in splits.items()},
        "strategy_evaluated": True, "paper_only": True,
        "settlement_label_provenance": "Gamma outcomePrices after resolution; no owner-scoped payout or balance receipt",
        "wallet_addresses_published": False,
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": True, "model_refit": False, "live_orders_enabled": False,
        "decision": "paper_whale_concentration_replay_pending_independent_settlement_cash_join",
        "next_action": "Do not admit a Canary from maker concentration; require owner-scoped settlement, cash release and reusable balance checkpoints.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with (output_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["start", "slug", "rows", "valid_exchange_rows", "net_up", "net_down", "selected_outcome", "top_maker_concentration", "entry_price", "winner"]
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
