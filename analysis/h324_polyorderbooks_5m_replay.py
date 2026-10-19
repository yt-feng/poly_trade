"""H324: bounded paper replay on a public BTC 5-minute L2 archive.

The source is a fixed public Parquet release, not an authenticated execution
record.  This module therefore admits only a paper diagnostic.  It freezes two
small-capital hypotheses before reading outcomes:

* ``complement_arb`` buys both outcomes when the observed ask pair remains
  profitable after the taker fee;
* ``book_pressure`` buys one side from a pre-entry top-of-book pressure gap.

Both are replayed on one chronological cash ledger with the five-share floor,
fee, settlement and cash-release clock.  The source's ``is_winning_outcome``
label is used only after the market end and is never treated as an official
Chainlink/PTB receipt or a Canary fill.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd


DATASET_REVISION = "da05a396d41d8065dd3712ef9d6c18f1cf03e7dd"
DATASET_URL = (
    "https://huggingface.co/datasets/polyorderbooks/"
    "polymarket-crypto-btc-5min-250ms-l2"
)
DECISION_SECONDS_TO_CLOSE = 120.0
INITIAL_CASH = 10.0
MIN_ORDER_SHARES = 5.0
FEE_RATE = 0.07
RELEASE_DELAY_SECONDS = 600.0
REQUIRED_FILLS = 10
FRACTIONS = (0.04, 0.10, 0.20)
DEPTH_FLOORS = (100.0, 150.0)
PRESSURE_THRESHOLD = 0.25
ARBITRAGE_MARGIN = 0.01


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _first_level(value: Any) -> float:
    if not isinstance(value, (list, tuple)) or not value:
        return float("nan")
    return float(value[0]) if _finite(value[0]) else float("nan")


def _as_epoch_seconds(value: Any) -> float:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return float(timestamp.timestamp())


def load_entries(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    required = {
        "market_slug", "coin", "contract_length", "token_id", "outcome",
        "is_winning_outcome", "market_end_at", "captured_at", "seconds_to_close",
        "bid_sizes", "ask_sizes", "best_bid", "best_ask", "crossed",
    }
    frame = pd.read_parquet(path)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("missing_columns:" + ",".join(missing))
    if frame.empty:
        raise ValueError("empty_dataset")
    if not frame["coin"].astype(str).str.lower().eq("btc").all():
        raise ValueError("non_btc_rows")
    if not frame["contract_length"].astype(str).eq("5m").all():
        raise ValueError("non_5m_rows")
    tte = pd.to_numeric(frame["seconds_to_close"], errors="coerce")
    if not tte.between(0.0, 300.0, inclusive="both").all():
        raise ValueError("time_to_close_out_of_range")
    captured = pd.to_datetime(frame["captured_at"], utc=True, errors="coerce")
    end = pd.to_datetime(frame["market_end_at"], utc=True, errors="coerce")
    if captured.isna().any() or end.isna().any():
        raise ValueError("invalid_timestamp")
    # The archive documents a 250ms grid.  Reject rows that cannot be placed on it.
    if not (captured.dt.microsecond % 250_000 == 0).all():
        raise ValueError("captured_at_not_250ms_grid")
    markets = frame.groupby("market_slug", sort=False)
    bad_market = []
    for market, group in markets:
        if group["token_id"].nunique() != 2 or group["outcome"].nunique() != 2:
            bad_market.append(str(market))
            continue
        wins = group.drop_duplicates("token_id").get("is_winning_outcome")
        if wins is None or int(pd.Series(wins).astype(bool).sum()) != 1:
            bad_market.append(str(market))
    if bad_market:
        raise ValueError("market_identity_or_winner_contract:" + str(len(bad_market)))

    frame = frame.copy()
    frame["tte_seconds"] = tte.astype(float)
    frame["captured_epoch"] = captured.map(lambda value: float(value.timestamp()))
    frame["end_epoch"] = end.map(lambda value: float(value.timestamp()))
    frame["decision_distance"] = (frame["tte_seconds"] - DECISION_SECONDS_TO_CLOSE).abs()
    # One observation per token at the frozen decision horizon.  A stable sort
    # makes ties deterministic and never uses rows after the decision.
    frame = frame.sort_values(
        ["market_slug", "token_id", "decision_distance", "captured_epoch"],
        kind="stable",
    )
    selected = frame.groupby(["market_slug", "token_id"], sort=False).head(1)
    rows: list[dict[str, Any]] = []
    for market, group in selected.groupby("market_slug", sort=False):
        if len(group) != 2:
            continue
        tokens = {str(row.outcome).lower(): row for row in group.itertuples()}
        if set(tokens) != {"up", "down"}:
            continue
        up, down = tokens["up"], tokens["down"]
        rows.append(
            {
                "market_slug": str(market),
                "entry_ts": float(min(up.captured_epoch, down.captured_epoch)),
                "end_ts": float(max(up.end_epoch, down.end_epoch)),
                "ask_up": float(up.best_ask) if _finite(up.best_ask) else float("nan"),
                "ask_down": float(down.best_ask) if _finite(down.best_ask) else float("nan"),
                "bid_up": float(up.best_bid) if _finite(up.best_bid) else float("nan"),
                "bid_down": float(down.best_bid) if _finite(down.best_bid) else float("nan"),
                "ask_size_up": _first_level(up.ask_sizes),
                "ask_size_down": _first_level(down.ask_sizes),
                "bid_size_up": _first_level(up.bid_sizes),
                "bid_size_down": _first_level(down.bid_sizes),
                "winner_up": bool(up.is_winning_outcome),
                "winner_down": bool(down.is_winning_outcome),
                "crossed_up": bool(up.crossed),
                "crossed_down": bool(down.crossed),
            }
        )
    entries = pd.DataFrame(rows).sort_values("end_ts", kind="stable").reset_index(drop=True)
    if entries.empty:
        raise ValueError("no_two_token_decision_rows")
    summary = {
        "raw_rows": int(len(frame)),
        "decision_markets": int(len(entries)),
        "source_revision": DATASET_REVISION,
        "decision_seconds_to_close": DECISION_SECONDS_TO_CLOSE,
        "winner_label_is_paper_only": True,
    }
    return entries, summary


def _pressure(row: pd.Series, side: str) -> float:
    bid = float(row[f"bid_size_{side}"])
    ask = float(row[f"ask_size_{side}"])
    if not (_finite(bid) and _finite(ask)) or bid + ask <= 0:
        return float("nan")
    return (bid - ask) / (bid + ask)


def _shares(cash: float, price: float, fraction: float, legs: int) -> float:
    if not (_finite(price) and price > 0):
        return 0.0
    budget = cash * fraction
    per_share = price * (1.0 + FEE_RATE)
    return math.floor(min(budget / (per_share * legs), cash / (per_share * legs)) * 1000.0) / 1000.0


def _paper_event(
    row: pd.Series,
    candidate: str,
    fraction: float,
    depth_floor: float,
    cash: float,
) -> dict[str, Any] | None:
    if row.crossed_up or row.crossed_down:
        return None
    if not all(_finite(row[f"ask_{side}"]) and _finite(row[f"ask_size_{side}"]) for side in ("up", "down")):
        return None
    if not all(float(row[f"ask_size_{side}"]) >= depth_floor for side in ("up", "down")):
        return None
    if candidate == "complement_arb":
        total = float(row.ask_up) + float(row.ask_down)
        if total + FEE_RATE * total > 1.0 - ARBITRAGE_MARGIN:
            return None
        shares = _shares(cash, total, fraction, 1)
        if shares < MIN_ORDER_SHARES:
            return None
        return {"legs": 2, "price": total, "shares": shares, "winner": True}
    pressure_up = _pressure(row, "up")
    pressure_down = _pressure(row, "down")
    if not (_finite(pressure_up) and _finite(pressure_down)):
        return None
    gap = pressure_up - pressure_down
    side = "up" if gap >= PRESSURE_THRESHOLD else "down" if gap <= -PRESSURE_THRESHOLD else ""
    if not side:
        return None
    price = float(row[f"ask_{side}"])
    shares = _shares(cash, price, fraction, 1)
    if shares < MIN_ORDER_SHARES:
        return None
    return {
        "legs": 1,
        "price": price,
        "shares": shares,
        "winner": bool(row[f"winner_{side}"]),
    }


def replay(entries: pd.DataFrame, candidate: str, fraction: float, depth_floor: float) -> dict[str, Any]:
    cash = INITIAL_CASH
    available_at = -float("inf")
    events: list[dict[str, Any]] = []
    blocked = 0
    for _, row in entries.iterrows():
        if float(row.end_ts) < available_at:
            blocked += 1
            continue
        event = _paper_event(row, candidate, fraction, depth_floor, cash)
        if event is None:
            continue
        total_cost = float(event["shares"]) * float(event["price"])
        if event["legs"] == 2:
            total_cost *= 1.0
        fee = total_cost * FEE_RATE
        if cash + 1e-12 < total_cost + fee:
            blocked += 1
            continue
        cash -= total_cost + fee
        payout = float(event["shares"]) if event["winner"] else 0.0
        cash = cash + payout
        available_at = float(row.end_ts) + RELEASE_DELAY_SECONDS
        event.update({"end_ts": float(row.end_ts), "entry_cash": cash, "fee": fee, "payout": payout})
        events.append(event)
    first = events[:REQUIRED_FILLS]
    return {
        "candidate": candidate,
        "fraction": fraction,
        "depth_floor": depth_floor,
        "fills": len(events),
        "first_ten_fills": len(first),
        "first_ten_ending_cash_usd": float(first[-1]["entry_cash"]) if len(first) == REQUIRED_FILLS else float(cash),
        "ending_cash_usd": float(cash),
        "minimum_cash_usd": float(min([INITIAL_CASH] + [float(event["entry_cash"]) for event in events])),
        "blocked_events": blocked,
        "paper_first_ten_passed": len(first) == REQUIRED_FILLS and float(first[-1]["entry_cash"]) >= INITIAL_CASH,
        "paper_target_growth_passed": len(first) == REQUIRED_FILLS and float(first[-1]["entry_cash"]) >= 1000.0,
    }


def evaluate(path: Path, output: Path) -> dict[str, Any]:
    entries, source_summary = load_entries(path)
    n = len(entries)
    train_end = max(1, int(n * 0.60))
    validation_end = max(train_end + 1, int(n * 0.80))
    splits = {
        "train": entries.iloc[:train_end],
        "validation": entries.iloc[train_end:validation_end],
        "test": entries.iloc[validation_end:],
    }
    rows = []
    for candidate in ("complement_arb", "book_pressure"):
        for fraction in FRACTIONS:
            for depth in DEPTH_FLOORS:
                for split, frame in splits.items():
                    row = replay(frame, candidate, fraction, depth)
                    row["split"] = split
                    rows.append(row)
    metrics = pd.DataFrame(rows)
    train = metrics[metrics["split"] == "train"]
    admissible = train[
        (train["first_ten_fills"] == REQUIRED_FILLS)
        & train["paper_first_ten_passed"]
    ].sort_values("first_ten_ending_cash_usd", ascending=False)
    selected = admissible.iloc[0].to_dict() if not admissible.empty else None
    holdout = metrics[metrics["split"].isin(["validation", "test"])]
    if selected:
        holdout = holdout[
            (holdout["candidate"] == selected["candidate"])
            & (holdout["fraction"] == float(selected["fraction"]))
            & (holdout["depth_floor"] == float(selected["depth_floor"]))
        ]
    report = {
        "format": "h324-polyorderbooks-5m-paper-replay-v1",
        "hypothesis_id": "H324-polyorderbooks-5m-complement-and-pressure",
        "dataset_url": DATASET_URL,
        "dataset_revision": DATASET_REVISION,
        "source_scope": "public 250ms BTC 5m L2 archive; paper replay only",
        "source_summary": source_summary,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_shares": MIN_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": RELEASE_DELAY_SECONDS,
            "same_ledger": True,
            "no_concatenation": True,
            "single_position_or_pair": True,
            "paper_only": True,
        },
        "hypotheses": {
            "complement_arb": "buy both outcomes at the frozen 120s horizon only when fee-adjusted pair cost is below 0.99",
            "book_pressure": "buy the side whose pre-entry top-book pressure gap is at least 0.25",
        },
        "splits": {key: int(len(value)) for key, value in splits.items()},
        "selection": selected,
        "holdout": holdout.to_dict(orient="records") if selected else [],
        "metrics": metrics.to_dict(orient="records"),
        "decision": "paper_replay_only_pending_private_review",
        "causal_outcome_provenance": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": True,
        "model_refit": False,
        "live_orders_enabled": False,
        "next_action": "Do not promote from this public label-only archive. If a paper branch survives, require an independent owner-scoped OrderFilled, official settlement, cash-release and balance bundle before any Canary consideration.",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    metrics.to_csv(output / "metrics.csv", index=False)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.input, args.output)
    print(json.dumps({
        "format": report["format"],
        "decision": report["decision"],
        "strategy_evaluated": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
