"""H489: one fixed bilateral-preflow filter on the H487 PMData protocol.

The only changed rule is causal: a two-sided passive pair may be submitted at
a book event only when each target leg has already seen a public SELL print at
or below its target price during the preceding 30 seconds.  The print is a
liquidity-availability proxy, never an account fill.  Fees, latency, queue
consumption, merge release, cash floor and the one evolving $10 ledger remain
the H487 values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

try:
    from .h468_pmdata_python_event_replay import (
        DATASET_REVISION,
        DATASET_SOURCE_REPO_COMMIT,
        _apply_book,
        _apply_price_change,
        _market_events,
        _normalized_trade,
        _to_ns,
        _empty_books,
    )
    from .h487_pmdata_maker_pair_replay import _candidate, _partition
except ImportError:  # pragma: no cover - standalone workflow invocation
    from h468_pmdata_python_event_replay import (
        DATASET_REVISION,
        DATASET_SOURCE_REPO_COMMIT,
        _apply_book,
        _apply_price_change,
        _market_events,
        _normalized_trade,
        _to_ns,
        _empty_books,
    )
    from h487_pmdata_maker_pair_replay import _candidate, _partition


INITIAL_CASH = 10.0
LOOKBACK_SECONDS = 30.0
LOOKBACK_NS = int(LOOKBACK_SECONDS * 1_000_000_000)
ENTRY_LATENCY_NS = 20_000_000
RESPONSE_LATENCY_NS = 20_000_000
CASH_RELEASE_DELAY_SECONDS = 600.0
REQUIRED_PAIRS = 10
EPS = 1e-10


def _consume_leg(leg: dict[str, Any], quantity: float) -> None:
    ahead = min(float(leg["queue_ahead"]), quantity)
    leg["queue_ahead"] -= ahead
    remainder = quantity - ahead
    if remainder > EPS:
        leg["remaining"] = max(0.0, float(leg["remaining"]) - remainder)


def _recently_consumable(recent: list[tuple[int, float]], now: int, price: float) -> bool:
    cutoff = now - LOOKBACK_NS
    return any(ts >= cutoff and trade_price <= price + EPS for ts, trade_price in recent)


def _run_market(l2: Any, trades: Any) -> dict[str, Any]:
    books = _empty_books()
    recent_sells: dict[str, list[tuple[int, float]]] = {"yes": [], "no": []}
    pending: dict[str, Any] | None = None
    preflow_rejected = 0
    for exch_ns, local_ns, kind, _identity, row in _market_events(l2, trades):
        cutoff = local_ns - LOOKBACK_NS
        for outcome in recent_sells:
            recent_sells[outcome] = [item for item in recent_sells[outcome] if item[0] >= cutoff]
        event_type = str(row.get("event_type") or "")
        if kind < 3:
            if event_type == "book":
                _apply_book(row, books)
            elif event_type == "price_change":
                _apply_price_change(row, books)
        else:
            normalized = _normalized_trade(row)
            if normalized is not None and pending is not None:
                outcome, price, quantity, side = normalized
                leg = pending["legs"].get(outcome)
                if (
                    leg is not None
                    and side == "SELL"
                    and local_ns >= pending["ready_local_ns"]
                    and price <= leg["price"] + EPS
                ):
                    _consume_leg(leg, quantity)
                    if all(float(item["remaining"]) <= EPS for item in pending["legs"].values()):
                        return {
                            "status": "filled",
                            "preflow_rejected": preflow_rejected,
                            "fill": {
                                "fill_ts_ns": exch_ns,
                                "ready_local_ns": pending["ready_local_ns"],
                                "yes_price": pending["legs"]["yes"]["price"],
                                "no_price": pending["legs"]["no"]["price"],
                                "quantity": pending["quantity"],
                                "pair_cost": pending["pair_cost"],
                                "yes_queue_ahead": pending["initial_yes_queue_ahead"],
                                "no_queue_ahead": pending["initial_no_queue_ahead"],
                                "order_status": "two_sided_trade_confirmed_queue_proxy",
                            },
                        }
            if normalized is not None:
                outcome, price, _quantity, side = normalized
                if side == "SELL" and outcome in recent_sells:
                    recent_sells[outcome].append((local_ns, price))
        if pending is None and kind < 3:
            candidate = _candidate(books)
            if candidate is not None:
                if not (
                    _recently_consumable(recent_sells["yes"], local_ns, candidate["yes_price"])
                    and _recently_consumable(recent_sells["no"], local_ns, candidate["no_price"])
                ):
                    preflow_rejected += 1
                    continue
                pending = {
                    "quantity": candidate["quantity"],
                    "pair_cost": candidate["pair_cost"],
                    "ready_local_ns": local_ns + ENTRY_LATENCY_NS + RESPONSE_LATENCY_NS,
                    "initial_yes_queue_ahead": candidate["yes_queue_ahead"],
                    "initial_no_queue_ahead": candidate["no_queue_ahead"],
                    "legs": {
                        "yes": {"price": candidate["yes_price"], "queue_ahead": candidate["yes_queue_ahead"], "remaining": candidate["quantity"]},
                        "no": {"price": candidate["no_price"], "queue_ahead": candidate["no_queue_ahead"], "remaining": candidate["quantity"]},
                    },
                }
    if pending is not None and any(float(item["remaining"]) < pending["quantity"] - EPS for item in pending["legs"].values()):
        return {"status": "partial_pair", "submitted": True, "preflow_rejected": preflow_rejected}
    return {"status": "no_fill", "submitted": pending is not None, "preflow_rejected": preflow_rejected}


def replay(l2_path: Path, trades_path: Path, markets_path: Path, fills_path: Path | None, output: Path) -> dict[str, Any]:
    import polars as pl

    markets = pl.read_parquet(markets_path).sort("start_time")
    l2 = pl.read_parquet(l2_path)
    trades = pl.read_parquet(trades_path)
    onchain_rows = int(pl.read_parquet(fills_path).height) if fills_path and fills_path.exists() else None
    names = set(markets.get_column("market_slug").cast(pl.Utf8).to_list())
    l2 = l2.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(names)))
    trades = trades.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(names)))
    archive_markets = set(l2.get_column("market_slug").cast(pl.Utf8).unique().to_list())
    markets = markets.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(archive_markets)))
    by_l2 = _partition(l2)
    by_trades = _partition(trades)
    cash = INITIAL_CASH
    locked: list[tuple[float, float]] = []
    chain: list[dict[str, Any]] = []
    skipped_before_release = no_pair_fill = partial_pair = insufficient_cash = preflow_rejected = 0
    for market in markets.iter_rows(named=True):
        start_seconds = _to_ns(market["start_time"]) / 1e9
        end_seconds = _to_ns(market["end_time"]) / 1e9
        for release_ts, amount in sorted(locked):
            if release_ts <= start_seconds + EPS:
                cash += amount
        locked = [(ts, amount) for ts, amount in locked if ts > start_seconds + EPS]
        if locked:
            skipped_before_release += 1
            continue
        slug = str(market["market_slug"])
        result = _run_market(by_l2[slug], by_trades.get(slug, pl.DataFrame()))
        preflow_rejected += int(result.get("preflow_rejected", 0))
        if result.get("status") != "filled":
            partial_pair += result.get("status") == "partial_pair"
            no_pair_fill += result.get("status") != "partial_pair"
            continue
        fill = result["fill"]
        quantity = float(fill["quantity"])
        cost = float(fill["pair_cost"])
        if cost > cash + EPS:
            insufficient_cash += 1
            continue
        before = cash
        cash -= cost
        release_ts = end_seconds + CASH_RELEASE_DELAY_SECONDS
        locked.append((release_ts, quantity))
        chain.append({
            "sequence": len(chain) + 1, "market_slug": slug,
            "fill_ts": float(fill["fill_ts_ns"]) / 1e9, "release_ts": release_ts,
            "yes_price": float(fill["yes_price"]), "no_price": float(fill["no_price"]),
            "quantity": quantity, "pair_cost": cost, "gross_merge_edge": quantity - cost,
            "cash_before": before, "cash_after_entry": cash, "cash_after_release": before - cost + quantity,
            "yes_queue_ahead": float(fill["yes_queue_ahead"]), "no_queue_ahead": float(fill["no_queue_ahead"]),
            "order_status": fill["order_status"],
        })
        if len(chain) >= REQUIRED_PAIRS:
            break
    for _release_ts, amount in sorted(locked):
        cash += amount
    first_ten = chain[:REQUIRED_PAIRS]
    report = {
        "format": "old-pmdata-h489-bilateral-preflow-pair-replay-v1",
        "hypothesis_id": "H489_PMDATA_BILATERAL_PREFLOW_FILTER",
        "dataset_revision": DATASET_REVISION,
        "dataset_source_repo_commit": DATASET_SOURCE_REPO_COMMIT,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "minimum_order_notional_usd_per_leg": 5.0,
            "minimum_pair_edge": 0.005, "lookback_seconds": LOOKBACK_SECONDS,
            "entry_latency_ns": ENTRY_LATENCY_NS, "response_latency_ns": RESPONSE_LATENCY_NS,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS, "required_consecutive_pairs": REQUIRED_PAIRS,
            "same_ledger": True, "no_reset": True, "no_concatenation": True,
            "delta_from_h487": "causal bilateral prior SELL preflow at both target prices",
            "paper_only": True,
        },
        "source": {"l2_rows": int(l2.height), "trade_rows": int(trades.height), "onchain_fill_rows": onchain_rows, "markets": int(markets.height), "fixed_archive": True},
        "paper": {
            "pairs": len(chain), "first_ten_pairs": len(first_ten), "first_ten_passed": len(first_ten) == REQUIRED_PAIRS,
            "ending_cash_usd": cash, "first_ten_ending_cash_usd": first_ten[-1]["cash_after_release"] if len(first_ten) == REQUIRED_PAIRS else cash,
            "skipped_before_release": skipped_before_release, "no_pair_fill_markets": no_pair_fill,
            "partial_pair_markets": partial_pair, "insufficient_cash_pairs": insufficient_cash,
            "preflow_rejected_candidates": preflow_rejected, "chain": first_ten,
        },
        "new_market_data_collected": False, "model_refit": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "paper_same_ledger_pair_rows": len(first_ten),
        "evidence_boundary": "fixed public PMData paper replay; causal bilateral preflow queue proxy; no owner-scoped order, settlement, cash-release or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    path = output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report["report_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--l2", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--fills", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.l2, args.trades, args.markets, args.fills, args.output), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
