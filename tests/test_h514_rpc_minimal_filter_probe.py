import json

from analysis.h514_rpc_minimal_filter_probe import _shape_filters, _probe_endpoint, probe


OWNER = "0x" + "11" * 20
BLOCK_HASH = "0x" + "22" * 32


def test_shape_filters_isolate_minimal_and_owner_bound_forms():
    names = [name for name, _ in _shape_filters(lower=100, upper=119, block_hash=BLOCK_HASH, owner=OWNER)]
    assert names == [
        "range_empty",
        "range_topic0",
        "range_address",
        "range_address_topic0",
        "blockhash_empty",
        "blockhash_topic0",
        "blockhash_address_topic0",
        "owner_range_topic0",
        "owner_range_address_topic0",
    ]


def test_probe_keeps_raw_owner_and_filter_values_out_of_result():
    calls = []

    def rpc(endpoint, method, params):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x100"
        if method == "eth_getBlockByNumber":
            return {"hash": BLOCK_HASH}
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    result = _probe_endpoint("https://example.invalid", [{"owner": OWNER}], block_span=20, rpc_call=rpc)
    assert result["block_hash_query_succeeded"] is True
    assert len(result["accounts"][0]["shape_reports"]) == 9
    serialized = json.dumps(result)
    assert OWNER.lower() not in serialized
    assert BLOCK_HASH.lower() not in serialized
    assert len(calls) == 11


def test_probe_redacted_summary(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps({"accounts": [{"owner": OWNER}]}))

    def fake_rpc(endpoint, method, params):
        if method == "eth_blockNumber":
            return "0x100"
        if method == "eth_getBlockByNumber":
            return {"hash": BLOCK_HASH}
        return []

    monkeypatch.setattr("analysis.h514_rpc_minimal_filter_probe._rpc_call", fake_rpc)
    result = probe(input_path, ["https://example.invalid"], output_path)
    assert result["new_market_data_collected"] is False
    assert json.loads(output_path.read_text())["same_ledger_compounding_rows"] == 0
