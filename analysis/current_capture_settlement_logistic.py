"""Fixed no-tuning cost-aware settlement model on completed captures."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from current_capture_official_outcome import _settlement_pnl, load_official_labels
from current_capture_quote_markout import _dec, _quote, load_rows


MIN_NOTIONAL = Decimal("5")
ASK_CAP = Decimal("0.70")
FEE_RATE = Decimal("0.07")
SLIPPAGE = Decimal("0.005")


def _features(row: dict[str, Any]) -> list[float] | None:
    micro = row.get("microstructure") if isinstance(row.get("microstructure"), dict) else {}
    twap = micro.get("twap60") if isinstance(micro.get("twap60"), dict) else {}
    rules = micro.get("rules") if isinstance(micro.get("rules"), dict) else {}
    twap_price = _dec(twap.get("price"))
    target = _dec(rules.get("published_price_to_beat"))
    up = _quote(row, "up")
    down = _quote(row, "down")
    values = [
        (twap_price / target - Decimal("1")) * Decimal("10000") if twap_price is not None and target else None,
        _dec(up.get("ask")), _dec(down.get("ask")),
        _dec(up.get("ask")) - _dec(up.get("bid")) if _dec(up.get("ask")) is not None and _dec(up.get("bid")) is not None else None,
        _dec(down.get("ask")) - _dec(down.get("bid")) if _dec(down.get("ask")) is not None and _dec(down.get("bid")) is not None else None,
        _dec(up.get("ask")) * _dec(up.get("ask_size")) if _dec(up.get("ask")) is not None and _dec(up.get("ask_size")) is not None else None,
        _dec(down.get("ask")) * _dec(down.get("ask_size")) if _dec(down.get("ask")) is not None and _dec(down.get("ask_size")) is not None else None,
        _dec(up.get("bid")), _dec(down.get("bid")),
    ]
    return [float(value) for value in values] if all(value is not None for value in values) else None


def _cost(ask: Decimal) -> Decimal:
    return ask + SLIPPAGE + FEE_RATE * ask * (Decimal("1") - ask)


def _side_for_prediction(row: dict[str, Any], probability_up: float) -> str | None:
    up_ask = _dec(_quote(row, "up").get("ask"))
    down_ask = _dec(_quote(row, "down").get("ask"))
    up_depth = _dec(_quote(row, "up").get("ask_size"))
    down_depth = _dec(_quote(row, "down").get("ask_size"))
    if None in (up_ask, down_ask, up_depth, down_depth):
        return None
    choices = []
    if up_ask <= ASK_CAP and up_ask * up_depth >= MIN_NOTIONAL:
        choices.append((Decimal(str(probability_up)) - _cost(up_ask), "up"))
    if down_ask <= ASK_CAP and down_ask * down_depth >= MIN_NOTIONAL:
        choices.append((Decimal(str(1 - probability_up)) - _cost(down_ask), "down"))
    positive = [choice for choice in choices if choice[0] > 0]
    return max(positive)[1] if positive else None


def evaluate(root: Path, *, tte_center: float = 50.0, tte_width: float = 5.0) -> dict[str, Any]:
    rows, _ = load_rows(root, tte_center=tte_center, tte_width=tte_width)
    outcomes = load_official_labels(root)
    records = []
    for row in rows:
        label = outcomes.get(row["slug"])
        if not label or label["available_at_ms"] <= row["sample_ms"]:
            continue
        features = _features(row)
        if features is not None:
            records.append((row, features, label["outcome"]))
    markets = sorted(row["slug"] for row, _, _ in records)
    split = len(markets) // 2
    train_slugs, test_slugs = set(markets[:split]), set(markets[split:])
    train = [record for record in records if record[0]["slug"] in train_slugs]
    test = [record for record in records if record[0]["slug"] in test_slugs]
    x_train = np.asarray([features for _, features, _ in train], dtype=float)
    y_train = np.asarray([outcome == "Up" for _, _, outcome in train], dtype=int)
    if len(set(y_train.tolist())) < 2:
        raise ValueError("training labels contain one class")
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, solver="liblinear", random_state=0, max_iter=1000))
    model.fit(x_train, y_train)
    metrics = {}
    for name, partition in (("train_selection", train), ("test", test)):
        probabilities = model.predict_proba(np.asarray([features for _, features, _ in partition], dtype=float))[:, 1]
        trades = wins = 0
        pnl = Decimal("0")
        missing = 0
        for (row, _, outcome), probability in zip(partition, probabilities):
            side = _side_for_prediction(row, float(probability))
            if side is None:
                missing += 1
                continue
            value = _settlement_pnl(row, side, outcome)
            if value is None:
                missing += 1
                continue
            trades += 1
            wins += int((side == "up" and outcome == "Up") or (side == "down" and outcome == "Down"))
            pnl += value
        metrics[name] = {
            "paper_settlement_trades": trades,
            "wins": wins,
            "paper_settlement_pnl": str(pnl.quantize(Decimal("0.000001"))),
            "rows_without_usable_signal": missing,
        }
    return {
        "schema_version": 1,
        "decision_window_seconds_before_end": [tte_center - tte_width, tte_center + tte_width],
        "feature_count": 8,
        "eligible_markets_with_complete_features": len(records),
        "train_selection_markets": len(train_slugs),
        "test_markets": len(test_slugs),
        "fixed_model": {"standard_scaler": True, "logistic_regression_C": 1.0, "solver": "liblinear", "tuning": False},
        "fixed_contract": {"minimum_notional": "5", "fee_rate": "0.07", "slippage": "0.005", "ask_cap": "0.70"},
        "metrics": metrics,
        "interpretation": {"official_outcomes_used_after_feature_freeze": True, "strategy_pnl": "paper_settlement_only", "official_outcomes_are_execution_receipts": False, "live_orders": False},
    }


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
    print(json.dumps(result["metrics"], sort_keys=True))


if __name__ == "__main__":
    main()
