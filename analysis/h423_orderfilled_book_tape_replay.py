"""Replay one fixed OrderFilled book-and-tape rule on the PMData public source.

The source is public population-level execution data. It is never treated as
our account's fills. The rule is fixed before the replay: at T-60 seconds, use
only earlier on-chain taker flow for that market, require three fills, $50
notional, 60% signed-flow dominance, and non-negative price improvement versus
the side's prior median. Buy exactly five shares at the last observed price
plus one tick, reserve the documented fee, and enforce the same 300s settlement
plus 600s cash-release clock. The output is a paper upper bound only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq

DATASET_ID = "mileswangs/polymarket-btc-updown-5m-orderbook-tick-data"
DATASET_REVISION = "acb8cecf4234677823ddd8ee1275de1db48aa995"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET_ID}"
TICK = 0.01
SHARES = 5.0
FEE_RATE = 0.07
SETTLEMENT_SECONDS = 300
CASH_RELEASE_SECONDS = 600
DECISION_SECONDS_BEFORE_END = 60


def _utc(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value).tz_localize("UTC") if pd.Timestamp(value).tzinfo is None else pd.Timestamp(value).tz_convert("UTC")


def read_inputs(input_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    markets = pq.read_table(input_dir / "markets.parquet").to_pandas()
    markets["start_time"] = pd.to_datetime(markets["start_time"], utc=True)
    markets["end_time"] = pd.to_datetime(markets["end_time"], utc=True)
    markets["winning_outcome"] = markets["winning_outcome"].astype(str).str.lower()
    dataset = ds.dataset(str(input_dir / "onchain"), format="parquet", partitioning="hive")
    fills = dataset.to_table(columns=[
        "market_slug", "timestamp", "outcome", "taker_side", "price", "usd_amount", "token_amount",
    ]).to_pandas()
    fills["timestamp"] = pd.to_datetime(fills["timestamp"], utc=True)
    fills["outcome"] = fills["outcome"].astype(str).str.lower()
    fills["taker_side"] = fills["taker_side"].astype(str).str.upper()
    for col in ("price", "usd_amount", "token_amount"):
        fills[col] = pd.to_numeric(fills[col], errors="coerce")
    fills = fills[(fills["price"] > 0) & (fills["price"] < 1) & (fills["usd_amount"] > 0)]
    fills = fills.sort_values(["market_slug", "timestamp"], kind="mergesort")
    return markets.sort_values("start_time", kind="mergesort"), fills


def candidate_from_history(market: pd.Series, history: pd.DataFrame) -> dict[str, Any] | None:
    if history.empty:
        return None
    history = history[history["outcome"].isin({"yes", "no"})].copy()
    if history.empty:
        return None
    history["signed_usd"] = history["usd_amount"].where(history["taker_side"] == "BUY", -history["usd_amount"])
    grouped = history.groupby("outcome", sort=True)
    summary = grouped.agg(
        net_usd=("signed_usd", "sum"),
        absolute_usd=("usd_amount", "sum"),
        fill_count=("usd_amount", "size"),
        last_price=("price", "last"),
        median_price=("price", "median"),
    )
    if summary.empty:
        return None
    side = str(summary["net_usd"].idxmax())
    row = summary.loc[side]
    total_abs = float(summary["absolute_usd"].sum())
    net = float(row["net_usd"])
    dominance = net / total_abs if total_abs > 0 else 0.0
    improvement_bps = (float(row["median_price"]) - float(row["last_price"])) / float(row["median_price"]) * 10000.0
    if net < 50.0 or float(row["fill_count"]) < 3 or dominance < 0.60 or improvement_bps < 0.0:
        return None
    entry_price = float(row["last_price"]) + TICK
    if not 0 < entry_price < 1:
        return None
    decision = _utc(market["end_time"]) - pd.Timedelta(seconds=DECISION_SECONDS_BEFORE_END)
    entry_ts = int(decision.timestamp() * 1000)
    entry_cost = SHARES * entry_price
    fee = SHARES * FEE_RATE * entry_price * (1.0 - entry_price)
    return {
        "slug": str(market["market_slug"]),
        "entry_ts": entry_ts,
        "cash_available_ts": entry_ts + (SETTLEMENT_SECONDS + CASH_RELEASE_SECONDS) * 1000,
        "signal": side,
        "winner": str(market["winning_outcome"]),
        "correct": side == str(market["winning_outcome"]),
        "fill_count": int(row["fill_count"]),
        "net_flow_usd": net,
        "flow_dominance": dominance,
        "price_improvement_bps": improvement_bps,
        "observed_last_price": float(row["last_price"]),
        "entry_price": entry_price,
        "entry_cost": entry_cost,
        "fee": fee,
        "payout": SHARES if side == str(market["winning_outcome"]) else 0.0,
        "status": "paper",
        "source": "public_orderfilled_population",
    }


def build_candidates(markets: pd.DataFrame, fills: pd.DataFrame) -> list[dict[str, Any]]:
    by_slug = {slug: group for slug, group in fills.groupby("market_slug", sort=False)}
    candidates: list[dict[str, Any]] = []
    for _, market in markets.iterrows():
        history = by_slug.get(str(market["market_slug"]))
        if history is None:
            continue
        start = _utc(market["start_time"])
        decision = _utc(market["end_time"]) - pd.Timedelta(seconds=DECISION_SECONDS_BEFORE_END)
        history = history[(history["timestamp"] >= start) & (history["timestamp"] < decision)]
        event = candidate_from_history(market, history)
        if event is not None:
            candidates.append(event)
    return sorted(candidates, key=lambda row: (row["entry_ts"], row["slug"]))


def evaluate_chain(events: list[dict[str, Any]], initial_cash: float = 10.0) -> dict[str, Any]:
    cash = float(initial_cash)
    selected: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    failure: str | None = None
    for event in events:
        if previous is not None:
            if int(event["entry_ts"]) <= int(previous["cash_available_ts"]):
                continue
            if float(previous["payout"]) <= 0:
                failure = "prior_cash_release_required"
                break
            cash += float(previous["payout"])
        if cash < SHARES:
            failure = "insufficient_cash_for_minimum_order"
            break
        reserved = float(event["entry_cost"]) + float(event["fee"])
        if reserved > cash:
            failure = "insufficient_cash_for_next_entry"
            break
        cash -= reserved
        selected.append(event)
        previous = event
    if previous is not None:
        cash += float(previous["payout"])
    return {
        "required_fills": 10,
        "fills": len(selected),
        "consecutive": True,
        "ending_cash": round(cash, 8),
        "net_pnl": round(cash - float(initial_cash), 8),
        "failure_reason": failure or (None if len(selected) >= 10 else "fewer_than_required_consecutive_fills"),
        "events": [event["slug"] for event in selected],
    }


def split_rows(markets: pd.DataFrame, candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    n = len(markets)
    train_end, validation_end = int(n * 0.60), int(n * 0.80)
    split_by_slug: dict[str, str] = {}
    market_slugs = markets["market_slug"].astype(str).tolist()
    for i, slug in enumerate(market_slugs):
        split_by_slug[slug] = "train" if i < train_end else "validation" if i < validation_end else "test"
    out = {"train": [], "validation": [], "test": []}
    for event in candidates:
        out[split_by_slug[event["slug"]]].append(event)
    return out


def run(input_dir: Path, output: Path) -> dict[str, Any]:
    markets, fills = read_inputs(input_dir)
    candidates = build_candidates(markets, fills)
    splits = split_rows(markets, candidates)
    market_slugs = markets["market_slug"].astype(str).tolist()
    train_end, validation_end = int(len(market_slugs) * 0.60), int(len(market_slugs) * 0.80)
    market_split = {slug: "train" if i < train_end else "validation" if i < validation_end else "test" for i, slug in enumerate(market_slugs)}
    market_counts = {name: sum(split == name for split in market_split.values()) for name in ("train", "validation", "test")}
    split_results = {name: {"markets": market_counts[name], "candidate_rows": len(rows), "direction_correct": sum(row["correct"] for row in rows), "direction_accuracy": (sum(row["correct"] for row in rows) / len(rows) if rows else None), "chain": evaluate_chain(rows)} for name, rows in splits.items()}
    report = {
        "format": "h423-orderfilled-book-tape-replay-v1",
        "source": {"dataset_id": DATASET_ID, "revision": DATASET_REVISION, "url": DATASET_URL, "input_dir": str(input_dir)},
        "source_scope": "public population-level on-chain fills; no owner-scoped account evidence",
        "protocol": {"initial_cash_usd": 10.0, "minimum_order_shares": SHARES, "taker_fee_rate": FEE_RATE, "settlement_seconds": SETTLEMENT_SECONDS, "cash_release_delay_seconds": CASH_RELEASE_SECONDS, "decision_seconds_before_end": DECISION_SECONDS_BEFORE_END, "same_ledger": True, "no_concatenation": True, "paper_only": True},
        "fixed_rule": {"history": "fills in [market_start, T-60s)", "min_fill_count": 3, "min_net_flow_usd": 50.0, "min_flow_dominance": 0.60, "min_price_improvement_bps": 0.0, "entry_price": "last observed side price plus one tick", "features": ["taker direction", "fill size", "price improvement"]},
        "input_rows": {"markets": int(len(markets)), "onchain_fills": int(len(fills))},
        "candidates": len(candidates),
        "direction_correct": sum(row["correct"] for row in candidates),
        "direction_accuracy": sum(row["correct"] for row in candidates) / len(candidates) if candidates else None,
        "splits": split_results,
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0, "live_orders_enabled": False, "model_refit": False, "new_market_data_collected": True,
        "decision": "paper_diagnostic_only_pending_owner_scoped_execution_and_settlement_bundle",
        "root_cause_boundary": "Public OrderFilled population features can test causal direction and capacity, but cannot prove this account's fill, settlement, cash release or balance continuity.",
        "next_action": "Promote only if a fixed test split reaches ten strict-release paper fills and an independent owner-scoped receipt bundle is available; otherwise retain as diagnostic."
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.input_dir, args.output)
    print(json.dumps({"format": report["format"], "candidates": report["candidates"], "direction_accuracy": report["direction_accuracy"], "splits": report["splits"], "decision": report["decision"]}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
