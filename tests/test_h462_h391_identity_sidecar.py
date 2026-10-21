import gzip
import json
from pathlib import Path

from analysis.h462_h391_identity_sidecar import build_sidecar, _digest


def _write(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _row(**overrides):
    row = {
        "id": "fill-1", "maker": "maker-1", "taker": "taker-1",
        "orderHash": "order-1", "transactionHash": "tx-1",
        "makerAmountFilled": "5000000", "takerAmountFilled": "5000000",
        "makerAssetId": "0", "takerAssetId": "token-1", "timestamp": "1", "fee": "0",
    }
    row.update(overrides)
    return row


def test_sidecar_is_deterministic_and_contains_no_raw_identity(tmp_path):
    fills = tmp_path / "fills"
    fills.mkdir()
    _write(fills / "1.fills.jsonl.gz", [_row(), _row(id="fill-2", orderHash="order-2", transactionHash="tx-2")])
    one = tmp_path / "one.jsonl.gz"
    two = tmp_path / "two.jsonl.gz"
    first = build_sidecar(fills, source_commit="source", dataset_revision="dataset", output=one)
    second = build_sidecar(fills, source_commit="source", dataset_revision="dataset", output=two)
    assert first["rows"]["total"] == 2
    assert first["sidecar"]["sha256"] == second["sidecar"]["sha256"]
    body = gzip.open(one, "rt", encoding="utf-8").read()
    assert "maker-1" not in body and "tx-1" not in body and "token-1" not in body
    assert _digest("maker-1") in body
    assert first["redaction"]["raw_values_emitted"] is False


def test_sidecar_keeps_numeric_fields_and_reports_missing_values(tmp_path):
    fills = tmp_path / "fills"
    fills.mkdir()
    _write(fills / "1.fills.jsonl.gz", [_row(fee="")])
    output = tmp_path / "sidecar.jsonl.gz"
    report = build_sidecar(fills, source_commit="source", dataset_revision="dataset", output=output)
    assert report["rows"]["complete_identity_rows"] == 0
    assert report["missing_raw_fields"] == {"fee": 1}
    row = json.loads(gzip.open(output, "rt", encoding="utf-8").readline())
    assert row["numeric"]["makerAmountFilled"] == "5000000"
    assert row["numeric"]["fee"] == ""
    assert set(row) == {"source_file", "identity_digest", "numeric"}
