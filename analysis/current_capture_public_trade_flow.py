"""Evaluate fixed public-trade-flow signals from completed raw captures."""

from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from current_capture_official_outcome import _settlement_pnl
from current_capture_quote_markout import _dec, _quote, load_rows


CANDIDATES = ("public_trade_imbalance_60s", "public_last_trade_direction")
WINDOW_SECONDS = 60
ASK_CAP = Decimal("0.70")
MIN_NOTIONAL = Decimal("5")


def _iter(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value


def load_public_trades(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Load only public trade crosschecks observed before each decision.

    The raw event's available_at_ms is the local observation clock. The
    embedded trade timestamp is a venue event clock and is separately bounded
    by the selected decision sample during evaluation.
    """
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(root.rglob("raw-*.jsonl.gz")):
        for event in _iter(path):
            if event.get("source") != "polymarket_public_trade_crosscheck":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            slug = payload.get("slug")
            response = payload.get("response")
            observed_ms = event.get("received_at_ns")
            observed_ms = observed_ms // 1_000_000 if isinstance(observed_ms, int) else None
            if not isinstance(slug, str) or not isinstance(response, list) or observed_ms is None:
                continue
            for trade in response:
                if not isinstance(trade, dict):
                    continue
                try:
                    timestamp_ms = int(trade["timestamp"]) * 1000
                    price = Decimal(str(trade["price"]))
                    size = Decimal(str(trade["size"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if timestamp_ms < 0 or price <= 0 or size <= 0:
                    continue
                outcome = trade.get("outcome")
                side = trade.get("side")
                if outcome not in {"Up", "Down"} or side not in {"BUY", "SELL"}:
                    continue
                result[slug].append({
                    "timestamp_ms": timestamp_ms,
                    "observed_ms": observed_ms,
                    "outcome": outcome,
                    "side": side,
                    "price": price,
                    "size": size,
                })
    return result


def _signal(trades: list[dict[str, Any]], decision_ms: int, candidate: str) -> str | None:
    eligible = [
        trade for trade in trades
        if decision_ms - WINDOW_SECONDS * 1000 <= trade["timestamp_ms"] <= decision_ms
        and trade["observed_ms"] <= decision_ms
    ]
    if not eligible:
        return None
    if candidate == "public_last_trade_direction":
        last = max(eligible, key=lambda trade: (trade["timestamp_ms"], trade["observed_ms"]))
        return last["outcome"]
    if candidate == "public_trade_imbalance_60s":
        signed = Decimal("0")
        for trade in eligible:
            amount = trade["price"] * trade["size"]
            signed += amount if trade["outcome"] == "Up" and trade["side"] == "BUY" else Decimal("0")
            signed -= amount if trade["outcome"] == "Down" and trade["side"] == "BUY" else Decimal("0")
            signed -= amount if trade["outcome"] == "Up" and trade["side"] == "SELL" else Decimal("0")
            signed += amount if trade["outcome"] == "Down" and trade["side"] == "SELL" else Decimal("0")
        if signed == 0:
            return None
        return "Up" if signed > 0 else "Down"
    raise ValueError(candidate)


def evaluate(root: Path, *, tte_center: float = 50.0, tte_width: float = 5.0) -> dict[str, Any]:
    rows, _ = load_rows(root, tte_center=tte_center, tte_width=tte_width)
    from current_capture_official_outcome import load_official_labels
    outcomes = load_official_labels(root)
    trades = load_public_trades(root)
    rows = [
        row for row in rows
        if row["slug"] in outcomes
        and outcomes[row["slug"]]["available_at_ms"] > row["sample_ms"]
    ]
    markets = sorted({row["slug"] for row in rows})
    split = len(markets) // 2
    partitions = {"train_selection": set(markets[:split]), "test": set(markets[split:])}
    result: dict[str, Any] = {
        "schema_version": 1,
        "decision_window_seconds_before_end": [tte_center - tte_width, tte_center + tte_width],
        "eligible_markets_with_official_outcome": len(markets),
        "train_selection_markets": len(partitions["train_selection"]),
        "test_markets": len(partitions["test"]),
        "raw_public_trade_slugs": len(trades),
        "fixed_contract": {"minimum_notional": "5", "fee_rate": "0.07", "slippage": "0.005", "ask_cap": "0.70"},
        "metrics": {},
    }
    for candidate in CANDIDATES:
        result["metrics"][candidate] = {}
        for partition, allowed in partitions.items():
            paper_trades = wins = 0
            pnl = Decimal("0")
            missing = 0
            for row in rows:
                if row["slug"] not in allowed:
                    continue
                side = _signal(trades.get(row["slug"], []), row["sample_ms"], candidate)
                if side is None:
                    missing += 1
                    continue
                quote = _quote(row, side)
                ask = _dec(quote.get("ask"))
                depth = _dec(quote.get("ask_size"))
                if ask is None or depth is None or ask > ASK_CAP or ask * depth < MIN_NOTIONAL:
                    missing += 1
                    continue
                outcome = outcomes[row["slug"]]["outcome"]
                value = _settlement_pnl(row, side.lower(), outcome)
                if value is None:
                    missing += 1
                    continue
                paper_trades += 1
                wins += int(side == outcome)
                pnl += value
            result["metrics"][candidate][partition] = {
                "paper_settlement_trades": paper_trades,
                "wins": wins,
                "paper_settlement_pnl": str(pnl.quantize(Decimal("0.000001"))),
                "rows_without_usable_signal": missing,
            }
    result["interpretation"] = {
        "raw_public_trade_events_used_before_decision": True,
        "official_outcomes_used_after_feature_freeze": True,
        "official_outcomes_are_execution_receipts": False,
        "strategy_pnl": "paper_settlement_only",
        "live_orders": False,
        "new_capture_collected": False,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tte-center", type=float, default=50.0)
    parser.add_argument("--tte-width", type=float, default=5.0)
    args = parser.parse_args()
    result = evaluate(args.input, tte_center=args.tte_center, tte_width=args.tte_width)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"eligible_markets": result["eligible_markets_with_official_outcome"], "raw_public_trade_slugs": result["raw_public_trade_slugs"], "metrics": result["metrics"]}, sort_keys=True))


if __name__ == "__main__":
    main()
