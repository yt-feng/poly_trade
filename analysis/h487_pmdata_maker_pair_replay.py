"""H487: fixed PMData two-sided passive complete-set replay.

This is a deliberately separate protocol from H468's one-sided 0.84 queue
proxy.  At a book snapshot it posts a YES bid and the complementary NO bid at
the displayed best bids.  Both legs must fill through later public SELL
prints, after the same frozen latency.  A complete pair is then merged into
one collateral unit after the market close plus the frozen release delay.

The replay is a public-tape queue proxy and remains paper-only.  It cannot
create an authenticated order, settlement, cash-release or Canary row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

try:
    from h468_pmdata_python_event_replay import (
        DATASET_REVISION,
        DATASET_SOURCE_REPO_COMMIT,
        _apply_book,
        _apply_price_change,
        _best,
        _empty_books,
        _market_events,
        _normalized_trade,
        _price_key,
        _to_ns,
        _winning_outcome,
    )
except ModuleNotFoundError:  # package import from the repository root
    from .h468_pmdata_python_event_replay import (
        DATASET_REVISION,
        DATASET_SOURCE_REPO_COMMIT,
        _apply_book,
        _apply_price_change,
        _best,
        _empty_books,
        _market_events,
        _normalized_trade,
        _price_key,
        _to_ns,
        _winning_outcome,
    )

INITIAL_CASH = 10.0
MIN_ORDER_NOTIONAL = 5.0
MINIMUM_SHARES = 5.0
MIN_PAIR_EDGE = 0.005
TICK_SIZE = 0.001
ENTRY_LATENCY_NS = 20_000_000
RESPONSE_LATENCY_NS = 20_000_000
CASH_RELEASE_DELAY_SECONDS = 600.0
REQUIRED_PAIRS = 10
EPS = 1e-10


def _pair_quantity(yes_bid: float, no_bid: float) -> float | None:
    """Smallest equal share quantity satisfying both five-dollar legs."""
    if not all(math.isfinite(value) and 0.0 < value < 1.0 for value in (yes_bid, no_bid)):
        return None
    return max(MINIMUM_SHARES, MIN_ORDER_NOTIONAL / yes_bid, MIN_ORDER_NOTIONAL / no_bid)


def _candidate(books: dict[str, Any]) -> dict[str, Any] | None:
    yes = _best(books["yes"])
    no = _best(books["no"])
    if yes is None or no is None:
        return None
    yes_bid, _yes_ask, yes_qty, _ = yes
    no_bid, _no_ask, no_qty, _ = no
    pair_cost = yes_bid + no_bid
    if pair_cost >= 1.0 - MIN_PAIR_EDGE:
        return None
    quantity = _pair_quantity(yes_bid, no_bid)
    if quantity is None or min(yes_qty, no_qty) + EPS < quantity:
        return None
    return {
        "yes_price": _price_key(yes_bid),
        "no_price": _price_key(no_bid),
        "quantity": float(quantity),
        "pair_cost": float(quantity * pair_cost),
        "yes_queue_ahead": float(books["yes"]["bids"].get(_price_key(yes_bid), 0.0)),
        "no_queue_ahead": float(books["no"]["bids"].get(_price_key(no_bid), 0.0)),
    }


def _consume_leg(leg: dict[str, Any], quantity: float) -> None:
    """Consume displayed queue, then our remaining order, on one public print."""
    ahead = min(float(leg["queue_ahead"]), quantity)
    leg["queue_ahead"] -= ahead
    remainder = quantity - ahead
    if remainder > EPS:
        leg["remaining"] = max(0.0, float(leg["remaining"]) - remainder)


def _run_market(l2: Any, trades: Any) -> dict[str, Any]:
    books = _empty_books()
    pending: dict[str, Any] | None = None
    for exch_ns, local_ns, kind, _identity, row in _market_events(l2, trades):
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
        if pending is None and kind < 3:
            candidate = _candidate(books)
            if candidate is not None:
                pending = {
                    "quantity": candidate["quantity"],
                    "pair_cost": candidate["pair_cost"],
                    "ready_local_ns": local_ns + ENTRY_LATENCY_NS + RESPONSE_LATENCY_NS,
                    "initial_yes_queue_ahead": candidate["yes_queue_ahead"],
                    "initial_no_queue_ahead": candidate["no_queue_ahead"],
                    "legs": {
                        "yes": {
                            "price": candidate["yes_price"],
                            "queue_ahead": candidate["yes_queue_ahead"],
                            "remaining": candidate["quantity"],
                        },
                        "no": {
                            "price": candidate["no_price"],
                            "queue_ahead": candidate["no_queue_ahead"],
                            "remaining": candidate["quantity"],
                        },
                    },
                }
    if pending is not None and any(float(item["remaining"]) < pending["quantity"] - EPS for item in pending["legs"].values()):
        return {"status": "partial_pair", "submitted": True}
    return {"status": "no_fill", "submitted": pending is not None}


def _partition(frame: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in frame.partition_by("market_slug", as_dict=True).items():
        result[str(key[0] if isinstance(key, tuple) else key)] = value
    return result


def replay(
    l2_path: Path,
    trades_path: Path,
    markets_path: Path,
    fills_path: Path | None,
    output: Path,
) -> dict[str, Any]:
    import polars as pl

    markets = pl.read_parquet(markets_path).sort("start_time")
    l2 = pl.read_parquet(l2_path)
    trades = pl.read_parquet(trades_path)
    onchain_rows = int(pl.read_parquet(fills_path).height) if fills_path and fills_path.exists() else None
    market_names = set(markets.get_column("market_slug").cast(pl.Utf8).to_list())
    l2 = l2.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(market_names)))
    trades = trades.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(market_names)))
    archive_markets = set(l2.get_column("market_slug").cast(pl.Utf8).unique().to_list())
    markets = markets.filter(pl.col("market_slug").cast(pl.Utf8).is_in(list(archive_markets)))

    by_l2 = _partition(l2)
    by_trades = _partition(trades)
    cash = INITIAL_CASH
    locked: list[tuple[float, float]] = []
    chain: list[dict[str, Any]] = []
    skipped_before_release = 0
    no_pair_fill = 0
    partial_pair = 0
    insufficient_cash = 0

    for market in markets.iter_rows(named=True):
        start_seconds = _to_ns(market["start_time"]) / 1e9
        end_seconds = _to_ns(market["end_time"]) / 1e9
        for release_ts, amount in sorted(locked):
            if release_ts <= start_seconds + EPS:
                cash += amount
            else:
                break
        locked = [(release_ts, amount) for release_ts, amount in locked if release_ts > start_seconds + EPS]
        if locked:
            skipped_before_release += 1
            continue
        slug = str(market["market_slug"])
        result = _run_market(by_l2[slug], by_trades.get(slug, pl.DataFrame()))
        if result.get("status") != "filled":
            if result.get("status") == "partial_pair":
                partial_pair += 1
            else:
                no_pair_fill += 1
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
            "sequence": len(chain) + 1,
            "market_slug": slug,
            "fill_ts": float(fill["fill_ts_ns"]) / 1e9,
            "release_ts": release_ts,
            "yes_price": float(fill["yes_price"]),
            "no_price": float(fill["no_price"]),
            "quantity": quantity,
            "pair_cost": cost,
            "gross_merge_edge": quantity - cost,
            "cash_before": before,
            "cash_after_entry": cash,
            "cash_after_release": before - cost + quantity,
            "yes_queue_ahead": float(fill["yes_queue_ahead"]),
            "no_queue_ahead": float(fill["no_queue_ahead"]),
            "order_status": fill["order_status"],
        })
        if len(chain) >= REQUIRED_PAIRS:
            break

    for _release_ts, amount in sorted(locked):
        cash += amount
    first_ten = chain[:REQUIRED_PAIRS]
    report = {
        "format": "old-pmdata-h487-maker-pair-replay-v1",
        "hypothesis_id": "H487_PMDATA_TWO_SIDED_PASSIVE_COMPLETE_SET",
        "dataset_revision": DATASET_REVISION,
        "dataset_source_repo_commit": DATASET_SOURCE_REPO_COMMIT,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MIN_ORDER_NOTIONAL,
            "minimum_shares": MINIMUM_SHARES,
            "minimum_pair_edge": MIN_PAIR_EDGE,
            "entry_latency_ns": ENTRY_LATENCY_NS,
            "response_latency_ns": RESPONSE_LATENCY_NS,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "required_consecutive_pairs": REQUIRED_PAIRS,
            "same_ledger": True,
            "no_reset": True,
            "no_concatenation": True,
            "post_only_both_legs": True,
            "maker_execution_fee": 0.0,
            "pair_merge": True,
            "paper_only": True,
            "execution_engine": "pure_python_two_sided_trade_confirmed_queue_proxy",
        },
        "source": {
            "l2_rows": int(l2.height),
            "trade_rows": int(trades.height),
            "onchain_fill_rows": onchain_rows,
            "markets": int(markets.height),
            "fixed_archive": True,
        },
        "paper": {
            "pairs": len(chain),
            "first_ten_pairs": len(first_ten),
            "first_ten_passed": len(first_ten) == REQUIRED_PAIRS,
            "ending_cash_usd": cash,
            "first_ten_ending_cash_usd": first_ten[-1]["cash_after_release"] if len(first_ten) == REQUIRED_PAIRS else cash,
            "skipped_before_release": skipped_before_release,
            "no_pair_fill_markets": no_pair_fill,
            "partial_pair_markets": partial_pair,
            "insufficient_cash_pairs": insufficient_cash,
            "chain": first_ten,
        },
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "paper_same_ledger_pair_rows": len(first_ten),
        "evidence_boundary": "fixed public PMData paper replay; two-sided queue proxy; no owner-scoped order, settlement, cash-release or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
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
