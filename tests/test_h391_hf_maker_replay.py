import gzip
import json
from pathlib import Path

from analysis.h391_hf_maker_replay import _fill_price_and_side, load_label, load_prints


def test_fill_price_and_side_uses_explicit_usdc_leg():
    token, price, shares, side = _fill_price_and_side({
        "makerAssetId": "0", "takerAssetId": "123",
        "makerAmountFilled": "2500000", "takerAmountFilled": "5000000",
    })
    assert (token, price, shares, side) == ("123", price, shares, "BUY")
    assert str(price) == "0.5"
    assert str(shares) == "5"


def test_load_prints_maps_token_and_preserves_causal_order(tmp_path: Path):
    start = 1772668800
    label_path = tmp_path / f"{start}.json"
    label_path.write_text(json.dumps([{
        "slug": f"btc-updown-5m-{start}",
        "markets": [{"slug": f"btc-updown-5m-{start}", "outcomes": '["Up", "Down"]',
                      "outcomePrices": '["1", "0"]', "clobTokenIds": '["11", "22"]'}],
    }]))
    label = load_label(label_path, start)
    fills = tmp_path / f"{start}.fills.jsonl.gz"
    rows = [
        {"timestamp": start + 2, "taker": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e", "makerAssetId": "0", "takerAssetId": "11", "makerAmountFilled": "2500000", "takerAmountFilled": "5000000"},
        {"timestamp": start + 1, "taker": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e", "makerAssetId": "22", "takerAssetId": "0", "makerAmountFilled": "5000000", "takerAmountFilled": "2500000"},
    ]
    with gzip.open(fills, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    class Print:
        def __init__(self, ts, outcome, side, price, size):
            self.ts, self.outcome, self.side, self.price, self.size = ts, outcome, side, price, size
    prints = load_prints(fills, label, Print)
    assert [row.ts for row in prints] == [start + 1, start + 2]
    assert [row.outcome for row in prints] == ["Down", "Up"]
    assert [row.side for row in prints] == ["SELL", "BUY"]
