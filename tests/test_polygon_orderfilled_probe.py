import pytest

from analysis.polygon_orderfilled_probe import (
    V1_ORDER_FILLED_TOPIC,
    decode_v1_log,
    decode_v2_log,
)


def word(value):
    return f"{value:064x}"


def log(topic, data, block=100, index=2):
    return {
        "address": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
        "topics": [topic, "0x" + word(7), "0x" + word(int("11" * 20, 16)), "0x" + word(int("22" * 20, 16))],
        "data": "0x" + "".join(word(value) for value in data),
        "transactionHash": "0x" + "aa" * 32,
        "blockNumber": hex(block),
        "logIndex": hex(index),
    }


def test_decode_v1_preserves_canonical_event_identity_and_amounts():
    result = decode_v1_log(log(V1_ORDER_FILLED_TOPIC, [101, 0, 5000000, 2500000, 700]), block_timestamp=123)
    assert result["event_id"] == "0x" + "aa" * 32 + ":2"
    assert result["maker"] == "0x" + "11" * 20
    assert result["taker"] == "0x" + "22" * 20
    assert result["maker_asset_id"] == "101"
    assert result["timestamp_ms"] == 123000


def test_decode_v1_rejects_wrong_topic_or_short_data():
    with pytest.raises(ValueError, match="not a V1"):
        decode_v1_log(log("0x" + "00" * 32, [1, 0, 1, 1, 0]))
    with pytest.raises(ValueError, match="missing"):
        decode_v1_log(log(V1_ORDER_FILLED_TOPIC, [1, 0, 1]))


def test_decode_v2_keeps_explicit_side_and_token_id():
    topic = "0x" + "bb" * 32
    result = decode_v2_log(log(topic, [0, 987, 2000000, 1000000, 0, 0, 0], block=101), topic=topic, block_timestamp=456)
    assert result["exchange_version"] == "v2"
    assert result["side"] == "0"
    assert result["token_id"] == "987"
    assert result["timestamp_ms"] == 456000

