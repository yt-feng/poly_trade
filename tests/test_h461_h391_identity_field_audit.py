import gzip
import json
from pathlib import Path

from analysis.h461_h391_identity_field_audit import audit


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


def test_audit_counts_identity_fields_without_emitting_values(tmp_path):
    _write(tmp_path / "1772668800.fills.jsonl.gz", [_row(), _row(id="fill-2", orderHash="order-2", transactionHash="tx-2")])
    result = audit(tmp_path, source_commit="source", dataset_revision="dataset")
    assert result["rows"]["total"] == 2
    assert result["rows"]["complete_identity_rows"] == 2
    assert result["identity_fields"]["orderHash"]["unique_values"] == 2
    assert "maker-1" not in json.dumps(result)
    assert "tx-1" not in json.dumps(result)
    assert result["identity_concentration"]["maker"]["unique_values"] == 1
    assert result["identity_concentration"]["maker"]["top1_share"] == 1.0


def test_audit_keeps_missing_field_and_execution_gap_separate(tmp_path):
    _write(tmp_path / "1772668800.fills.jsonl.gz", [_row(fee="")])
    result = audit(tmp_path, source_commit="source", dataset_revision="dataset")
    assert result["rows"]["complete_identity_rows"] == 0
    assert result["missing_required_field_rows"] == {"fee": 1}
    assert "queue_position" in result["missing_for_causal_execution"]
