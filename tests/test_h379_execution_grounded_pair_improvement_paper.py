import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

from h379_execution_grounded_pair_improvement_paper import (  # noqa: E402
    ENTRY_SECONDS,
    PAIR_PRICE_IMPROVEMENT_THRESHOLD,
    load_execution_edge,
    replay,
)


EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"


def row(token: str, price: str, shares: str, timestamp: int) -> dict[str, str]:
    scale = 1_000_000
    return {
        "taker": EXCHANGE,
        "timestamp": str(timestamp),
        "makerAssetId": "0",
        "takerAssetId": token,
        "makerAmountFilled": str(int(float(price) * float(shares) * scale)),
        "takerAmountFilled": str(int(float(shares) * scale)),
    }


def test_execution_edge_uses_only_pre_entry_rows_and_requires_pair_improvement(tmp_path):
    path = tmp_path / "100.fills.jsonl.gz"
    rows = [
        row("1", "0.45", "20", 110),
        row("2", "0.48", "5", 115),
        row("1", "0.99", "20", 250),  # excluded: after the 120-second entry clock
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")
    label = {"start": 100, "end": 400, "slug": "btc-updown-5m-100", "token_by_outcome": {"Up": "1", "Down": "2"}, "winner": "Up"}
    result = load_execution_edge(path, label)
    assert result["valid_exchange_rows"] == 2
    assert result["selected_outcome"] == "Up"
    assert result["pair_price_improvement"] >= PAIR_PRICE_IMPROVEMENT_THRESHOLD
    assert result["entry_price"] == 0.45


def test_replay_releases_payout_before_reusing_cash():
    events = [
        {"end": 100, "slug": "a", "selected_outcome": "Up", "entry_price": 0.45, "winner": "Up"},
        {"end": 101, "slug": "b", "selected_outcome": "Up", "entry_price": 0.45, "winner": "Up"},
        {"end": 701, "slug": "c", "selected_outcome": "Up", "entry_price": 0.45, "winner": "Up"},
    ]
    result = replay(events)
    assert result["fills"] == 2
    assert result["blocked_cash_events"] == 1
    assert result["ending_cash_usd"] > 10
