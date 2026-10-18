"""Run a preregistered quote-markout paper comparison on an existing capture.

The output is explicitly not fill PnL. Labels are joined only after features
are frozen, and the chronological market split is fixed before evaluation.
"""

from __future__ import annotations

import argparse
import gzip
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


MIN_NOTIONAL = Decimal("5")
FEE_RATE = Decimal("0.07")
SLIPPAGE = Decimal("0.005")
ASK_CAP = Decimal("0.70")
HORIZONS = (5, 30)


def _iter(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value


def _dec(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _quote(row: dict[str, Any], side: str) -> dict[str, Any] | None:
    value = row.get(side)
    return value if isinstance(value, dict) else None


def _side_markout(side: str, up_change: Any) -> Decimal | None:
    change = _dec(up_change)
    if change is None:
        return None
    return change if side == "up" else -change


def _pnl(quote: dict[str, Any], markout: Decimal) -> Decimal | None:
    ask = _dec(quote.get("ask"))
    bid = _dec(quote.get("bid"))
    if ask is None or bid is None or ask <= 0 or bid <= 0:
        return None
    depth = _dec(quote.get("ask_size"))
    if depth is None or ask * depth < MIN_NOTIONAL:
        return None
    quantity = MIN_NOTIONAL / ask
    fee_per_share = FEE_RATE * ask * (Decimal("1") - ask)
    # A conservative quote markout: future mid change less the entry half
    # spread, fixed slippage, and the recorded fee formula.
    entry_half_spread = (ask - bid) / Decimal("2")
    return quantity * (markout - entry_half_spread - SLIPPAGE - fee_per_share)


def load_rows(root: Path, *, tte_center: float = 20.0, tte_width: float = 5.0) -> tuple[list[dict[str, Any]], dict[tuple[str, int, int], dict[str, Any]]]:
    best_by_market: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("snapshots-*.jsonl.gz")):
        for row in _iter(path):
            micro = row.get("microstructure")
            if not isinstance(micro, dict):
                continue
            tte = micro.get("time_to_expiry_seconds")
            if not isinstance(tte, (int, float)) or not tte_center - tte_width <= float(tte) <= tte_center + tte_width:
                continue
            if row.get("poly_valid") is not True or row.get("pair_quote_valid") is not True:
                continue
            if not isinstance(row.get("slug"), str) or not isinstance(row.get("sample_ms"), int):
                continue
            if _quote(row, "up") is None or _quote(row, "down") is None:
                continue
            slug = row["slug"]
            current = best_by_market.get(slug)
            # Collapse the 15-25 second window to one decision snapshot per
            # market. Replaying every one-second sample would manufacture
            # repeated entries for the same contract.
            if current is None or (
                abs(float(micro["time_to_expiry_seconds"]) - tte_center),
                -int(row["sample_ms"]),
            ) < (
                abs(float(current["microstructure"]["time_to_expiry_seconds"]) - 20.0),
                -int(current["sample_ms"]),
            ):
                best_by_market[slug] = row
    labels: dict[tuple[str, int, int], dict[str, Any]] = {}
    for path in sorted(root.rglob("labels-*.jsonl.gz")):
        for label in _iter(path):
            horizon = label.get("horizon_seconds")
            if isinstance(label.get("slug"), str) and isinstance(label.get("prediction_ms"), int) and isinstance(horizon, int):
                labels[(label["slug"], label["prediction_ms"], horizon)] = label
    return list(best_by_market.values()), labels


def _candidate_side(row: dict[str, Any], candidate: str) -> str | None:
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
        raise ValueError(f"unknown candidate: {candidate}")
    return side if ask <= ASK_CAP else None


def evaluate(rows: list[dict[str, Any]], labels: dict[tuple[str, int, int], dict[str, Any]]) -> dict[str, Any]:
    markets = sorted({row["slug"] for row in rows})
    split = len(markets) // 2
    partitions = {"train_selection": set(markets[:split]), "test": set(markets[split:])}
    result: dict[str, Any] = {
        "candidate_set": ["target_sign_ask_le_0.70", "lower_ask_ask_le_0.70"],
        "market_count": len(markets),
        "train_selection_markets": len(partitions["train_selection"]),
        "test_markets": len(partitions["test"]),
        "fixed_contract": {"minimum_notional": "5", "fee_rate": "0.07", "slippage": "0.005", "ask_cap": "0.70"},
        "metrics": {},
    }
    for candidate in result["candidate_set"]:
        result["metrics"][candidate] = {}
        for partition, allowed in partitions.items():
            part_rows = [r for r in rows if r["slug"] in allowed]
            result["metrics"][candidate][partition] = {}
            for horizon in HORIZONS:
                trades = 0
                pnl = Decimal("0")
                missing = 0
                for row in part_rows:
                    side = _candidate_side(row, candidate)
                    if side is None:
                        continue
                    label = labels.get((row["slug"], row["sample_ms"], horizon))
                    if not label or label.get("kind") != "quote_markout_not_fill_pnl":
                        missing += 1
                        continue
                    markout = _side_markout(side, label.get("up_mid_change"))
                    if markout is None:
                        missing += 1
                        continue
                    value = _pnl(_quote(row, side), markout)
                    if value is None:
                        missing += 1
                        continue
                    trades += 1
                    pnl += value
                result["metrics"][candidate][partition][str(horizon)] = {
                    "quote_markout_trades": trades,
                    "quote_markout_pnl": str(pnl.quantize(Decimal("0.000001"))),
                    "rows_without_usable_markout": missing,
                }
    result["interpretation"] = {
        "strategy_pnl": "not_computed",
        "quote_markout_is_fill_pnl": False,
        "official_outcomes_used": False,
        "live_orders": False,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tte-center", type=float, default=20.0)
    parser.add_argument("--tte-width", type=float, default=5.0)
    args = parser.parse_args()
    rows, labels = load_rows(args.input, tte_center=args.tte_center, tte_width=args.tte_width)
    result = evaluate(rows, labels)
    result["decision_window_seconds_before_end"] = [args.tte_center - args.tte_width, args.tte_center + args.tte_width]
    result["decision_window_rows_after_pair_quote_gate"] = len(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "markets": result["market_count"], "metrics": result["metrics"]}, sort_keys=True))


if __name__ == "__main__":
    main()
