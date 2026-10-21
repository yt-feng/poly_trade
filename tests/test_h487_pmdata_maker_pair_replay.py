from pathlib import Path

import polars as pl

from analysis.h487_pmdata_maker_pair_replay import (
    REQUIRED_PAIRS,
    _candidate,
    _empty_books,
    _run_market,
    replay,
)


def _book(ts: int, slug: str) -> dict:
    return {
        "market_slug": slug,
        "timestamp": ts,
        "local_timestamp": ts,
        "event_type": "book",
        "bid_prices": [0.49],
        "bid_sizes": [20.0],
        "ask_prices": [0.51],
        "ask_sizes": [20.0],
        "pc_side": None,
        "pc_price": None,
        "pc_size": None,
    }


def _sell(ts: int, slug: str, outcome: str) -> dict:
    return {
        "market_slug": slug,
        "timestamp": ts,
        "local_timestamp": ts,
        "outcome": outcome,
        "side": "SELL",
        "price": 0.49,
        "size": 100.0,
    }


def test_complementary_best_bids_require_a_real_pair_edge():
    books = _empty_books()
    books["yes"]["bids"][0.49] = 20.0
    books["yes"]["asks"][0.51] = 20.0
    books["no"]["bids"][0.49] = 20.0
    books["no"]["asks"][0.51] = 20.0
    candidate = _candidate(books)
    assert candidate is not None
    assert candidate["yes_price"] == 0.49
    assert candidate["no_price"] == 0.49
    assert candidate["quantity"] > 10.0

    books["yes"]["bids"].clear()
    books["yes"]["asks"].clear()
    books["yes"]["bids"][0.50] = 20.0
    books["yes"]["asks"][0.50] = 20.0
    books["no"]["bids"][0.50] = 20.0
    books["no"]["asks"][0.50] = 20.0
    assert _candidate(books) is None  # complementary bids sum to one


def test_both_legs_must_fill_before_pair_is_admitted():
    slug = "btc-updown-5m-1000"
    l2 = pl.DataFrame([_book(0, slug)])
    yes_only = pl.DataFrame([_sell(1, slug, "yes")])
    result = _run_market(l2, yes_only)
    assert result["status"] == "partial_pair"

    both = pl.DataFrame([_sell(1, slug, "yes"), _sell(2, slug, "no")])
    result = _run_market(l2, both)
    assert result["status"] == "filled"
    assert result["fill"]["order_status"] == "two_sided_trade_confirmed_queue_proxy"


def test_ten_pairs_use_one_evolving_cash_ledger(tmp_path: Path):
    l2_rows = []
    trade_rows = []
    markets = []
    for index in range(REQUIRED_PAIRS):
        start = index * 1000
        slug = f"btc-updown-5m-{start}"
        markets.append({"market_slug": slug, "start_time": start, "end_time": start + 300, "winning_outcome": "yes"})
        l2_rows.append(_book(start, slug))
        trade_rows.extend([_sell(start + 1, slug, "yes"), _sell(start + 2, slug, "no")])
    l2_path = tmp_path / "l2.parquet"
    trades_path = tmp_path / "trades.parquet"
    markets_path = tmp_path / "markets.parquet"
    pl.DataFrame(l2_rows).write_parquet(l2_path)
    pl.DataFrame(trade_rows).write_parquet(trades_path)
    pl.DataFrame(markets).write_parquet(markets_path)

    report = replay(l2_path, trades_path, markets_path, None, tmp_path / "results")
    assert report["paper"]["first_ten_passed"] is True
    assert report["paper"]["pairs"] == REQUIRED_PAIRS
    assert report["paper"]["first_ten_ending_cash_usd"] > 10.0
    assert report["actual_fills"] == 0
    assert report["verified_canary_fills"] == 0
    assert report["same_ledger_compounding_rows"] == 0
