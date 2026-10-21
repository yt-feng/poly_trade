import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import h467_pmdata_queue_latency_replay as h467
import h468_pmdata_python_event_replay as h468


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


def test_h468_normalizes_epoch_values_and_complementary_trades():
    assert h468._to_ns(1.5) == 1_500_000_000
    assert h468._to_ns(1_500_000_000) == 1_500_000_000_000_000_000
    assert h468._normalized_trade({"outcome": "yes", "price": 0.84, "size": 5, "side": "SELL"}) == ("yes", 0.84, 5.0, "SELL")
    assert h468._normalized_trade({"outcome": "no", "price": 0.16, "size": 5, "side": "BUY"}) == ("no", 0.84, 5.0, "SELL")


def test_h468_requires_post_latency_trade_to_fill_queue_proxy():
    class Frame:
        def __init__(self, rows):
            self.rows = rows

        def iter_rows(self, named=False):
            assert named
            return iter(self.rows)

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
