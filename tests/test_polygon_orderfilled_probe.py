from urllib.error import HTTPError

import pytest

import analysis.polygon_orderfilled_probe as probe
from analysis.polygon_orderfilled_probe import (
    V1_ORDER_FILLED_TOPIC,
    V2_ORDER_FILLED_TOPIC,
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
    result = decode_v2_log(log(V2_ORDER_FILLED_TOPIC, [0, 987, 2000000, 1000000, 0, 0, 0], block=101), topic=V2_ORDER_FILLED_TOPIC, block_timestamp=456)
    assert result["exchange_version"] == "v2"
    assert result["side"] == "0"
    assert result["token_id"] == "987"
    assert result["timestamp_ms"] == 456000


def test_rpc_uses_json_headers_and_exposes_bounded_http_status(monkeypatch):
    seen = {}

    def rejected(request, timeout):
        seen["headers"] = dict(request.headers)
        seen["timeout"] = timeout
        raise HTTPError(request.full_url, 403, "forbidden", {}, None)

    monkeypatch.setattr(probe, "urlopen", rejected)
    with pytest.raises(RuntimeError, match="^polygon_rpc_http_403$"):
        probe._rpc("https://rpc.example", "eth_blockNumber", [], 1)
    assert seen["headers"]["Accept"] == "application/json"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["headers"]["User-agent"] == probe.RPC_USER_AGENT
    assert seen["timeout"] == 30


def test_probe_defaults_to_v1_and_v2_exchange_sources(monkeypatch):
    calls = []

    def fake_rpc(url, method, params, request_id):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x10000"
        if method == "eth_getBlockByNumber":
            return {"number": "0x10000"}
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    monkeypatch.setattr(probe, "_rpc", fake_rpc)
    result = probe.probe(from_block=0xFF00, to_block=0xFF01, window_blocks=2, chunk_blocks=200)
    filters = [params[0] for method, params in calls if method == "eth_getLogs"]
    assert len(filters) == len(probe.DEFAULT_EXCHANGE_ADDRESSES)
    assert {item["address"] for item in filters} == set(probe.DEFAULT_EXCHANGE_ADDRESSES)
    assert all(item["topics"] == [list(probe.ORDER_FILLED_TOPICS)] for item in filters)
    assert result["topics"] == list(probe.ORDER_FILLED_TOPICS)
    assert result["exchange_versions"] == ["v1", "v2"]


def test_probe_can_filter_indexed_taker_topic(monkeypatch):
    calls = []

    def fake_rpc(url, method, params, request_id):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x10000"
        if method == "eth_getBlockByNumber":
            return {"number": "0x10000"}
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    monkeypatch.setattr(probe, "_rpc", fake_rpc)
    owner = "0x" + "22" * 20
    result = probe.probe(
        from_block=0xFF00,
        to_block=0xFF01,
        window_blocks=2,
        chunk_blocks=200,
        taker_addresses=[owner],
    )
    filters = [params[0] for method, params in calls if method == "eth_getLogs"]
    assert filters
    assert all(item["topics"] == [
        list(probe.ORDER_FILLED_TOPICS), None, None,
        "0x" + "0" * 24 + "22" * 20,
    ] for item in filters)
    assert result["taker_topic_filter"] == "0x" + "0" * 24 + "22" * 20
    assert result["account_scope"] == "public_taker_filtered; not a private wallet receipt"

