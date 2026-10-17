"""Evaluate fixed capture signals against post-resolution official labels.

This is a paper settlement audit. It never treats an official label as an
execution receipt and never sends an order.
"""

from __future__ import annotations

import argparse
import gzip
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from current_capture_quote_markout import _dec, _quote, load_rows


ASK_CAP = Decimal("0.70")
MIN_NOTIONAL = Decimal("5")
FEE_RATE = Decimal("0.07")
SLIPPAGE = Decimal("0.005")
CANDIDATES = ("target_sign_ask_le_0.70", "lower_ask_ask_le_0.70")


def _iter(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value


def load_official_labels(root: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("labels-*.jsonl.gz")):
        for row in _iter(path):
            kind = row.get("kind")
            if kind == "official_clob_winner":
                outcome = row.get("winning_outcome")
            elif kind == "market_resolved":
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                outcome = payload.get("winning_outcome")
            else:
                continue
            if not isinstance(row.get("slug"), str) or row["slug"] in labels:
                continue
            available_at = row.get("available_at_ms")
            if outcome in {"Up", "Down"} and isinstance(available_at, int):
                labels[row["slug"]] = {"outcome": outcome, "available_at_ms": available_at}
    return labels


def _side(row: dict[str, Any], candidate: str) -> str | None:
    if candidate == "lower_ask_ask_le_0.70":
        up = _dec(_quote(row, "up").get("ask"))
        down = _dec(_quote(row, "down").get("ask"))
        if up is None or down is None:
            return None
        side, ask = ("up", up) if up <= down else ("down", down)
    elif candidate == "target_sign_ask_le_0.70":
        micro = row.get("microstructure") if isinstance(row.get("microstructure"), dict) else {}
        twap = micro.get("twap60") if isinstance(micro.get("twap60"), dict) else {}
        rules = micro.get("rules") if isinstance(micro.get("rules"), dict) else {}
        twap_price = _dec(twap.get("price"))
        target = _dec(rules.get("published_price_to_beat"))
        if twap_price is None or target is None or target <= 0:
            return None
        side = "up" if twap_price >= target else "down"
        ask = _dec(_quote(row, side).get("ask"))
        if ask is None:
            return None
    else:
        raise ValueError(candidate)
    depth = _dec(_quote(row, side).get("ask_size"))
    if ask > ASK_CAP or depth is None or ask * depth < MIN_NOTIONAL:
        return None
    return side


def _settlement_pnl(row: dict[str, Any], side: str, outcome: str) -> Decimal | None:
    quote = _quote(row, side)
    ask = _dec(quote.get("ask"))
    if ask is None or ask <= 0:
        return None
    quantity = MIN_NOTIONAL / ask
    fee_per_share = FEE_RATE * ask * (Decimal("1") - ask)
    entry = ask + SLIPPAGE
    payoff = Decimal("1") if (side == "up" and outcome == "Up") or (side == "down" and outcome == "Down") else Decimal("0")
    return quantity * (payoff - entry - fee_per_share)


def evaluate(root: Path, *, tte_center: float = 50.0, tte_width: float = 5.0) -> dict[str, Any]:
    rows, _ = load_rows(root, tte_center=tte_center, tte_width=tte_width)
    outcomes = load_official_labels(root)
    rows = [
        row for row in rows
        if row["slug"] in outcomes
        and outcomes[row["slug"]]["available_at_ms"] > row["sample_ms"]
    ]
    markets = sorted({row["slug"] for row in rows})
    split = len(markets) // 2
    partitions = {"train_selection": set(markets[:split]), "test": set(markets[split:])}
    result = {
        "schema_version": 1,
        "decision_window_seconds_before_end": [tte_center - tte_width, tte_center + tte_width],
        "eligible_markets_with_official_outcome": len(markets),
        "official_outcomes": {
            k: sum(value["outcome"] == k for value in outcomes.values())
            for k in ("Up", "Down")
        },
        "train_selection_markets": len(partitions["train_selection"]),
        "test_markets": len(partitions["test"]),
        "fixed_contract": {"minimum_notional": "5", "fee_rate": "0.07", "slippage": "0.005", "ask_cap": "0.70"},
        "metrics": {},
    }
    for candidate in CANDIDATES:
        result["metrics"][candidate] = {}
        for partition, allowed in partitions.items():
            trades = 0
            wins = 0
            pnl = Decimal("0")
            missing = 0
            for row in rows:
                if row["slug"] not in allowed:
                    continue
                side = _side(row, candidate)
                if side is None:
                    missing += 1
                    continue
                outcome = outcomes[row["slug"]]["outcome"]
                value = _settlement_pnl(row, side, outcome)
                if value is None:
                    missing += 1
                    continue
                trades += 1
                wins += int((side == "up" and outcome == "Up") or (side == "down" and outcome == "Down"))
                pnl += value
            result["metrics"][candidate][partition] = {
                "paper_settlement_trades": trades,
                "wins": wins,
                "paper_settlement_pnl": str(pnl.quantize(Decimal("0.000001"))),
                "rows_without_usable_signal": missing,
            }
    result["interpretation"] = {
        "official_outcomes_used_after_feature_freeze": True,
        "official_outcome_is_execution_receipt": False,
        "strategy_pnl": "paper_settlement_only",
        "live_orders": False,
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
    print(json.dumps({"eligible_markets": result["eligible_markets_with_official_outcome"], "metrics": result["metrics"]}, sort_keys=True))


if __name__ == "__main__":
    main()
