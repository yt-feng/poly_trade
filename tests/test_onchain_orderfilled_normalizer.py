import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from onchain_orderfilled_normalizer import (
    deduplicate_orderfilled,
    normalize_orderfilled,
    quote_join_ready,
)


def base(**extra):
    result = {
        "block_number": 100,
        "tx_hash": "0x" + "aa" * 32,
        "log_index": 2,
        "order_hash": "0x" + "bb" * 32,
        "maker": "0x" + "11" * 20,
        "taker": "0x" + "22" * 20,
        "maker_amount_filled": 1_000_000,
        "taker_amount_filled": 600_000,
        "fee": 0,
    }
    result.update(extra)
    return result


def test_v1_buy_uses_taker_usdc_and_maker_token():
    out = normalize_orderfilled(base(maker_asset_id="123", taker_asset_id="0"), exchange_version="v1")
    assert out["side"] == "BUY"
    assert out["token_id"] == "123"
    assert out["sign"] == 1
    assert out["price"] == "0.6"


def test_v1_sell_uses_maker_usdc_and_taker_token():
    out = normalize_orderfilled(
        base(maker_asset_id="0", taker_asset_id="456", maker_amount_filled=600_000, taker_amount_filled=1_000_000),
        exchange_version="v1",
    )
    assert out["side"] == "SELL"
    assert out["token_id"] == "456"
    assert out["sign"] == -1
    assert out["price"] == "0.6"


def test_v2_explicit_side_and_token_are_normalized():
    out = normalize_orderfilled(base(side=0, token_id="789"), exchange_version="v2")
    assert out["side"] == "BUY"
    assert out["token_id"] == "789"
    assert out["price"] == "0.6"


def test_reverted_match_is_excluded():
    with pytest.raises(ValueError, match="reverted_transaction"):
        normalize_orderfilled(base(maker_asset_id="123", taker_asset_id="0", receipt_status=0), exchange_version="v1")


def test_conflicting_duplicate_event_fails_closed():
    row = normalize_orderfilled(base(maker_asset_id="123", taker_asset_id="0"), exchange_version="v1")
    other = dict(row, price="0.61")
    with pytest.raises(ValueError, match="conflicting_duplicate_event"):
        deduplicate_orderfilled([row, other])


def test_quote_join_requires_token_and_timestamp_identity():
    fill = {"token_id": "123", "market_id": "m", "timestamp_ms": 1000}
    assert quote_join_ready(fill, {"token_id": "123", "market_id": "m", "timestamp_ms": 2500})
    assert not quote_join_ready(fill, {"token_id": "123", "market_id": "m", "timestamp_ms": 4000})
    assert not quote_join_ready(fill, {"token_id": "999", "market_id": "m", "timestamp_ms": 1000})
