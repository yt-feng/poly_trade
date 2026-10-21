import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import h467_pmdata_queue_latency_replay as h467
import h468_pmdata_python_event_replay as h468
import h469_public_onchain_crosscheck as h469


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def iter_rows(self, named=False):
        assert named
        return iter(self.rows)


def token_book(outcome):
    # Mirror the YES-axis L2 input so either token has bid .84, ask .86.
    bid, ask = (0.84, 0.86) if outcome == "yes" else (0.14, 0.16)
    return Frame([
        {"timestamp": 0.0, "local_timestamp": 0.0, "event_type": "book",
         "bid_prices": [bid], "bid_sizes": [10.0],
         "ask_prices": [ask], "ask_sizes": [10.0]},
    ])


def token_trade(outcome, side="SELL", price=0.84, size=15.0, timestamp=0.05):
    return {"timestamp": timestamp, "local_timestamp": timestamp,
            "outcome": outcome, "side": side, "price": price, "size": size}


def test_contract_is_fixed_to_one_evolving_ledger():
    assert h467.INITIAL_CASH == 10.0
    assert h467.MINIMUM_SHARES == 5.0
    assert h467.REQUIRED_FILLS == 10
    assert h467.CASH_RELEASE_DELAY_SECONDS == 600.0
    assert h467.DATASET_REVISION == "acb8cecf4234677823ddd8ee1275de1db48aa995"
    assert h467.DATASET_SOURCE_REPO_COMMIT == "b7f29614a695740c2102760a4dacfb85b8d6e623"


def test_winning_outcome_normalization():
    assert h467._winning_outcome("yes") == "yes"
    assert h467._winning_outcome("NO") == "no"
    assert h467._winning_outcome("unknown") is None


def test_pmdata_market_book_has_complementary_token_mapping():
    assert h467._invert_winning_outcome("yes") == "no"
    assert h467._invert_winning_outcome("NO") == "yes"
    assert h467._invert_winning_outcome(None) is None


def test_h468_normalizes_epoch_values():
    assert h468._to_ns(1.5) == 1_500_000_000
    assert h468._to_ns(1_500_000_000) == 1_500_000_000_000_000_000


@pytest.mark.parametrize("outcome,normalized", [("Yes", "yes"), ("NO", "no"), ("up", "yes"), ("down", "no")])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_h468_trade_normalization_preserves_selected_token_units(outcome, normalized, side):
    row = {"outcome": outcome, "price": "0.8404", "size": "5", "side": side}
    before = dict(row)
    assert h468._normalized_trade(row) == (normalized, 0.84, 5.0, side.upper())
    assert row == before


@pytest.mark.parametrize("outcome", ["yes", "no"])
@pytest.mark.parametrize("side,price,expected", [
    ("SELL", 0.84, "filled"),
    ("BUY", 0.84, "no_fill"),
    ("SELL", 0.16, "filled"),
    ("BUY", 0.16, "no_fill"),
    ("BUY", 0.86, "no_fill"),
    ("SELL", 0.86, "no_fill"),
])
def test_h468_mirrored_token_queues_require_same_token_sell(outcome, side, price, expected):
    result = h468._run_market(token_book(outcome), Frame([token_trade(outcome, side, price)]))
    assert result["status"] == expected
    if expected == "filled":
        assert result["fill"] == {
            "outcome": outcome, "exec_qty": 5.0, "exec_price": 0.84,
            "fill_ts_ns": 50_000_000, "ready_local_ns": 40_000_000,
            "queue_ahead": 10.0, "order_status": "trade_confirmed_queue_proxy",
        }
    else:
        assert result["submitted"] is True


@pytest.mark.parametrize("outcome", ["yes", "no"])
def test_h468_matching_sells_consume_nonzero_queue_before_filling(outcome):
    first = token_trade(outcome, size=10.0)
    second = token_trade(outcome, size=5.0, timestamp=0.06)
    assert h468._run_market(token_book(outcome), Frame([first]))["status"] == "no_fill"
    result = h468._run_market(token_book(outcome), Frame([first, second]))
    assert result["status"] == "filled"
    assert result["fill"]["fill_ts_ns"] == 60_000_000
    assert result["fill"]["queue_ahead"] == 10.0


@pytest.mark.parametrize("outcome", ["yes", "no"])
@pytest.mark.parametrize("ignored", ["buy", "other_token", "above_limit", "before_ready"])
def test_h468_ineligible_trades_do_not_deplete_queue(outcome, ignored):
    first = token_trade(outcome, size=100.0)
    if ignored == "buy":
        first["side"] = "BUY"
    elif ignored == "other_token":
        first["outcome"] = "no" if outcome == "yes" else "yes"
    elif ignored == "above_limit":
        first["price"] = 0.86
    else:
        first.update(timestamp=0.01, local_timestamp=0.01)
    last = token_trade(outcome, size=5.0, timestamp=0.06)
    assert h468._run_market(token_book(outcome), Frame([first, last])) == {
        "status": "no_fill", "submitted": True,
    }


def test_h468_requires_post_latency_trade_to_fill_queue_proxy():
    l2 = Frame([
        {"timestamp": 0.0, "local_timestamp": 0.0, "event_type": "book",
         "bid_prices": [0.83], "bid_sizes": [10.0], "ask_prices": [0.85], "ask_sizes": [10.0]},
    ])
    early_trade = Frame([
        {"timestamp": 0.01, "local_timestamp": 0.01, "outcome": "yes", "price": 0.84, "size": 5.0, "side": "SELL"},
    ])
    late_trade = Frame([
        {"timestamp": 0.05, "local_timestamp": 0.05, "outcome": "yes", "price": 0.84, "size": 15.0, "side": "SELL"},
    ])
    assert h468._run_market(l2, early_trade)["status"] == "no_fill"
    result = h468._run_market(l2, late_trade)
    assert result["status"] == "filled"
    assert result["fill"]["outcome"] == "yes"
    assert result["fill"]["exec_qty"] == 5.0
    assert result["fill"]["queue_ahead"] == 0.0


def test_h469_public_match_requires_market_price_time_and_quantity():
    proxy = {"market_slug": "m", "outcome": "no", "fill_ts": 10.0, "exec_price": 0.84, "exec_qty": 5.0}
    rows = [
        {"market_slug": "m", "outcome": "NO", "timestamp_seconds": 11.0, "price": 0.84, "token_amount": 5.0},
        {"market_slug": "m", "outcome": "NO", "timestamp_seconds": 20.0, "price": 0.84, "token_amount": 100.0},
    ]
    assert h469._public_match_from_rows(proxy, rows)["public_match"] is True
    rows[0]["token_amount"] = 4.99
    assert h469._public_match_from_rows(proxy, rows)["public_match"] is False


@pytest.mark.parametrize("outcome", ["yes", "no"])
def test_h469_matches_h468_fill_in_same_token_price_units(outcome):
    result = h468._run_market(token_book(outcome), Frame([token_trade(outcome)]))
    assert result["status"] == "filled"
    fill = result["fill"]
    proxy = {"market_slug": "synthetic", "outcome": fill["outcome"],
             "fill_ts": fill["fill_ts_ns"] / 1e9,
             "exec_price": fill["exec_price"], "exec_qty": fill["exec_qty"]}
    row = {"market_slug": "synthetic", "outcome": outcome,
           "timestamp_seconds": 0.05, "price": 0.84, "token_amount": 5.0}
    assert h469._public_match_from_rows(proxy, [row])["public_match"] is True
    wrong_price = dict(row, price=0.16)
    wrong_token = dict(row, outcome="no" if outcome == "yes" else "yes")
    assert h469._public_match_from_rows(proxy, [wrong_price, wrong_token])["public_match"] is False
