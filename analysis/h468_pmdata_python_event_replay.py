"""H468: safe Python event-driven queue/latency replay on the fixed PMData day.

H467 showed that the pinned Rust backtester cannot be used reliably in this
runner: dependency installation and the archive load succeed, but the native
replay process segfaults before it can emit a report.  H468 keeps the same
archive, trigger, fee, latency, release clock and one evolving $10 ledger, and
replaces only the execution engine with a transparent Python event loop.

The fill rule is deliberately explicit and conservative for this diagnostic:
an order is submitted at the frozen 0.84 limit after the observation and
response delay, then requires a later PMData SELL trade at or below the limit
to consume the displayed queue ahead.  This is a queue proxy, not authenticated
account evidence, and it cannot create a Canary row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

DATASET_REVISION = "acb8cecf4234677823ddd8ee1275de1db48aa995"
DATASET_SOURCE_REPO_COMMIT = "b7f29614a695740c2102760a4dacfb85b8d6e623"
INITIAL_CASH = 10.0
MINIMUM_SHARES = 5.0
TRIGGER = 0.84
TICK_SIZE = 0.001
TAKER_FEE_RATE = 0.07
ENTRY_LATENCY_NS = 20_000_000
RESPONSE_LATENCY_NS = 20_000_000
CASH_RELEASE_DELAY_SECONDS = 600.0
REQUIRED_FILLS = 10


def _winning_outcome(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"yes", "up", "true", "1"}:
        return "yes"
    if text in {"no", "down", "false", "0"}:
        return "no"
    return None


def _to_ns(value: Any) -> int:
    """Normalize PMData datetime or numeric epoch values to nanoseconds."""
    if value is None:
        return 0
    if hasattr(value, "timestamp"):
        return int(round(float(value.timestamp()) * 1_000_000_000))
    number = float(value)
    magnitude = abs(number)
    if magnitude < 1e11:  # seconds
        return int(round(number * 1_000_000_000))
    if magnitude < 1e14:  # milliseconds
        return int(round(number * 1_000_000))
    if magnitude < 1e17:  # microseconds
        return int(round(number * 1_000))
    return int(round(number))


def _price_key(value: Any) -> float:
    return round(float(value), 3)


def _empty_books() -> dict[str, dict[str, dict[float, float]]]:
    return {
        "yes": {"bids": {}, "asks": {}},
        "no": {"bids": {}, "asks": {}},
    }


def _set_level(book: dict[float, float], price: Any, size: Any) -> None:
    if price is None or size is None:
        return
    px = _price_key(price)
    qty = float(size)
    if not math.isfinite(px) or not math.isfinite(qty) or qty <= 0:
        book.pop(px, None)
    else:
        book[px] = qty


def _apply_book(row: dict[str, Any], books: dict[str, Any]) -> None:
    """Replace both complementary token books from one PMData snapshot."""
    books.clear()
    books.update(_empty_books())
    bids = row.get("bid_prices") or []
    bid_sizes = row.get("bid_sizes") or []
    asks = row.get("ask_prices") or []
    ask_sizes = row.get("ask_sizes") or []
    for px, qty in zip(bids, bid_sizes):
        _set_level(books["yes"]["bids"], px, qty)
        _set_level(books["no"]["asks"], 1.0 - float(px), qty)
    for px, qty in zip(asks, ask_sizes):
        _set_level(books["yes"]["asks"], px, qty)
        _set_level(books["no"]["bids"], 1.0 - float(px), qty)


def _apply_price_change(row: dict[str, Any], books: dict[str, Any]) -> None:
    side = str(row.get("pc_side") or "").upper()
    price = row.get("pc_price")
    size = row.get("pc_size")
    if price is None or side not in {"BUY", "SELL"}:
        return
    yes_side = "bids" if side == "BUY" else "asks"
    no_side = "asks" if side == "BUY" else "bids"
    _set_level(books["yes"][yes_side], price, size)
    _set_level(books["no"][no_side], 1.0 - float(price), size)


def _best(book: dict[str, dict[float, float]]) -> tuple[float, float, float, float] | None:
    if not book["bids"] or not book["asks"]:
        return None
    bid = max(book["bids"])
    ask = min(book["asks"])
    if bid <= 0 or ask <= 0 or ask >= 1:
        return None
    return bid, ask, book["bids"][bid], book["asks"][ask]


def _normalized_trade(row: dict[str, Any]) -> tuple[str, float, float, str] | None:
    """Keep price and aggressor side in the selected outcome token's units."""
    outcome = _winning_outcome(row.get("outcome"))
    side = str(row.get("side") or "").upper()
    price = row.get("price")
    size = row.get("size")
    if outcome is None or side not in {"BUY", "SELL"} or price is None or size is None:
        return None
    px = float(price)
    qty = float(size)
    if not math.isfinite(px) or not math.isfinite(qty) or qty <= 0:
        return None
    return outcome, _price_key(px), qty, side


def _market_events(l2: Any, trades: Any) -> list[tuple[int, int, int, str, dict[str, Any]]]:
    events: list[tuple[int, int, int, str, dict[str, Any]]] = []
    for index, row in enumerate(l2.iter_rows(named=True)):
        ts = _to_ns(row.get("timestamp"))
        local = _to_ns(row.get("local_timestamp", row.get("timestamp")))
        kind = {"book": 0, "price_change": 1, "market_resolved": 2}.get(
            str(row.get("event_type") or ""), 2
        )
        events.append((ts, local, kind, f"l2-{index}", row))
    for index, row in enumerate(trades.iter_rows(named=True)):
        if _normalized_trade(row) is None:
            continue
        ts = _to_ns(row.get("timestamp"))
        local = _to_ns(row.get("local_timestamp", row.get("timestamp")))
        events.append((ts, local, 3, f"trade-{index}", row))
    events.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return events


def _run_market(l2: Any, trades: Any, trigger: float = TRIGGER) -> dict[str, Any]:
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
                if (
                    outcome == pending["outcome"]
                    and side == "SELL"
                    and local_ns >= pending["ready_local_ns"]
                    and price <= pending["price"] + 1e-9
                ):
                    ahead = pending["queue_ahead"]
                    consumed = min(ahead, quantity)
                    pending["queue_ahead"] = ahead - consumed
                    remainder = quantity - consumed
                    if remainder + 1e-12 >= pending["quantity"]:
                        return {
                            "status": "filled",
                            "fill": {
                                "outcome": outcome,
                                "exec_qty": pending["quantity"],
                                "exec_price": pending["price"],
                                "fill_ts_ns": exch_ns,
                                "ready_local_ns": pending["ready_local_ns"],
                                "queue_ahead": pending["initial_queue_ahead"],
                                "order_status": "trade_confirmed_queue_proxy",
                            },
                        }
        if pending is None and kind < 3:
            for outcome in ("yes", "no"):
                best = _best(books[outcome])
                if best is None:
                    continue
                bid, ask, _bid_qty, _ask_qty = best
                if (bid + ask) / 2.0 < trigger:
                    continue
                price = _price_key(trigger)
                initial_ahead = float(books[outcome]["bids"].get(price, 0.0))
                pending = {
                    "outcome": outcome,
                    "price": price,
                    "quantity": MINIMUM_SHARES,
                    "queue_ahead": initial_ahead,
                    "initial_queue_ahead": initial_ahead,
                    "ready_local_ns": local_ns + ENTRY_LATENCY_NS + RESPONSE_LATENCY_NS,
                }
                break
    return {"status": "no_fill", "submitted": pending is not None}


def replay(
    l2_path: Path,
    trades_path: Path,
    markets_path: Path,
    fills_path: Path | None,
    output: Path,
    trigger: float = TRIGGER,
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

    def partition(frame: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in frame.partition_by("market_slug", as_dict=True).items():
            result[str(key[0] if isinstance(key, tuple) else key)] = value
        return result

    by_l2 = partition(l2)
    by_trades = partition(trades)
    cash = INITIAL_CASH
    available_at = -float("inf")
    chain: list[dict[str, Any]] = []
    skipped_before_release = 0
    no_fill = 0
    insufficient_cash = 0
    for market in markets.iter_rows(named=True):
        slug = str(market["market_slug"])
        start_ns = _to_ns(market["start_time"])
        end_ns = _to_ns(market["end_time"])
        if start_ns / 1e9 < available_at:
            skipped_before_release += 1
            continue
        result = _run_market(by_l2[slug], by_trades.get(slug, pl.DataFrame()), trigger)
        if result.get("status") != "filled":
            no_fill += 1
            continue
        fill = result["fill"]
        price = float(fill["exec_price"])
        qty = float(fill["exec_qty"])
        cost = price * qty
        fee = qty * TAKER_FEE_RATE * price * (1.0 - price)
        total = cost + fee
        if total > cash + 1e-12:
            insufficient_cash += 1
            continue
        before = cash
        cash -= total
        winner = _winning_outcome(market.get("winning_outcome"))
        payout = qty if winner == fill["outcome"] else 0.0
        cash += payout
        release_ts = end_ns / 1e9 + CASH_RELEASE_DELAY_SECONDS
        available_at = release_ts
        chain.append({
            "sequence": len(chain) + 1,
            "market_slug": slug,
            "entry_ts": start_ns / 1e9,
            "fill_ts": float(fill["fill_ts_ns"]) / 1e9,
            "release_ts": release_ts,
            "outcome": fill["outcome"],
            "winner": bool(payout > 0),
            "exec_qty": qty,
            "exec_price": price,
            "cost": cost,
            "fee": fee,
            "cash_before": before,
            "cash_after": cash,
            "queue_ahead": float(fill["queue_ahead"]),
            "order_status": fill["order_status"],
        })
        if len(chain) >= REQUIRED_FILLS:
            break

    first_ten = chain[:REQUIRED_FILLS]
    report = {
        "format": "old-pmdata-h468-python-event-replay-v1",
        "hypothesis_id": "H468_PMDATA_PYTHON_QUEUE_LATENCY_PROXY",
        "dataset_revision": DATASET_REVISION,
        "dataset_source_repo_commit": DATASET_SOURCE_REPO_COMMIT,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_shares": MINIMUM_SHARES,
            "trigger_mid": trigger,
            "tick_size": TICK_SIZE,
            "entry_latency_ns": ENTRY_LATENCY_NS,
            "response_latency_ns": RESPONSE_LATENCY_NS,
            "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "required_consecutive_fills": REQUIRED_FILLS,
            "same_ledger": True,
            "no_reset": True,
            "paper_only": True,
            "execution_engine": "pure_python_trade_confirmed_queue_proxy",
        },
        "source": {
            "l2_rows": int(l2.height),
            "trade_rows": int(trades.height),
            "onchain_fill_rows": onchain_rows,
            "markets": int(markets.height),
            "fixed_archive": True,
        },
        "prior_executor_evidence": {
            "h467_run_ids": [39616705024, 39617843510, 39618926978],
            "h467_conclusion": "reproducible_native_executor_defect",
            "h467_report_produced": False,
        },
        "paper": {
            "fills": len(chain),
            "first_ten_fills": len(first_ten),
            "first_ten_passed": len(first_ten) == REQUIRED_FILLS,
            "ending_cash_usd": cash,
            "first_ten_ending_cash_usd": first_ten[-1]["cash_after"] if len(first_ten) == REQUIRED_FILLS else cash,
            "skipped_before_release": skipped_before_release,
            "no_fill_markets": no_fill,
            "insufficient_cash_fills": insufficient_cash,
            "chain": first_ten,
        },
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "fixed public PMData paper replay; trade-confirmed queue proxy; no owner-scoped order, settlement, cash-release or balance receipt",
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
    parser.add_argument("--trigger", type=float, default=TRIGGER)
    args = parser.parse_args()
    print(json.dumps(replay(args.l2, args.trades, args.markets, args.fills, args.output, args.trigger), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
