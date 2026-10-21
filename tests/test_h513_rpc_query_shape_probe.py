import json
from pathlib import Path

from analysis.h513_rpc_query_shape_probe import _filter_shapes, _probe_endpoint, probe


OWNER = "0x" + "11" * 20


def test_filter_shapes_cover_or_and_scalar_variants():
    shapes = [name for name, _ in _filter_shapes(OWNER, 100, 119)]
    assert shapes == [
        "event_or_address_or",
        "event_scalar_address_or_topic",
        "event_scalar_address_scalar_topic",
        "transfer_or_address",
        "transfer_scalar_address",
    ]


def test_probe_records_categories_without_publishing_owner_or_logs(tmp_path):
    calls = []

    def rpc(endpoint, method, params):
        calls.append((endpoint, method, params))
        if method == "eth_blockNumber":
            return "0x100"
        if isinstance(params[0].get("address"), list):
            raise RuntimeError("history_rpc_error:http_400")
        return []

    result = _probe_endpoint("https://example.invalid", [{"owner": OWNER}], block_span=20, rpc_call=rpc)
    assert result["block_number"] == 256
    assert result["accounts"][0]["categories"] == {"http_400": 2, "ok": 3}
    serialized = json.dumps(result["accounts"])
    assert OWNER.lower() not in serialized
    assert "0x11" * 20 not in serialized
    assert "logs" not in serialized
    assert len(calls) == 6


def test_probe_writes_redacted_summary(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps({"accounts": [{"owner": OWNER}]}))
    def fake_rpc(endpoint, method, params):
        return "0x100" if method == "eth_blockNumber" else []
    monkeypatch.setattr("analysis.h513_rpc_query_shape_probe._rpc_call", fake_rpc)
    result = probe(input_path, ["https://example.invalid"], output_path, block_span=20)
    assert result["new_market_data_collected"] is False
    assert json.loads(output_path.read_text())["actual_fills"] == 0
