import json

from analysis.h517_rpc_block_span_matrix import _probe_endpoint, probe


def test_probe_records_first_provider_boundary_without_raw_logs():
    calls = []

    def rpc(endpoint, method, params):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x100"
        query = params[0]
        span = int(query["toBlock"], 16) - int(query["fromBlock"], 16) + 1
        if span > 100:
            raise RuntimeError("history_rpc_error:http_400")
        return [{"sensitive": "not persisted"}]

    result = _probe_endpoint("https://example.invalid", [20, 100, 250], rpc_call=rpc)
    assert result["spans"] == [
        {"span": 20, "category": "ok", "row_count": 1},
        {"span": 100, "category": "ok", "row_count": 1},
        {"span": 250, "category": "http_400", "row_count": 0},
    ]
    assert all("sensitive" not in json.dumps(item) for item in result["spans"])
    assert len(calls) == 4


def test_probe_writes_redacted_matrix(tmp_path):
    output = tmp_path / "matrix.json"

    def rpc(endpoint, method, params):
        return "0x100" if method == "eth_blockNumber" else []

    result = probe(["https://example.invalid"], [100, 20, 20], output, rpc_call=rpc)
    assert result["spans"] == [20, 100]
    assert json.loads(output.read_text())["actual_fills"] == 0
