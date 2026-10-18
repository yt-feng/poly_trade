import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h77_authoritative_receipt_bundle_audit import audit


def write_csv(path: Path, header: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(header)


def test_quote_schema_is_not_authoritative_receipt_schema(tmp_path):
    write_csv(tmp_path / "quotes.csv", ["ts_iso", "target_price", "final_price"])
    result = audit(tmp_path)
    assert result["csv_files_scanned"] == 1
    assert result["quote_label_field_file_counts"] == {"final_price": 1, "target_price": 1}
    assert result["authoritative_receipt_files"] == 0
    assert result["decision"] == "source_insufficient_for_authoritative_receipt_chain"


def test_receipt_fields_are_counted_but_do_not_become_fills(tmp_path):
    write_csv(tmp_path / "fake-receipts.csv", ["owner", "trade_id", "transaction_hash", "cash_available_ms"])
    result = audit(tmp_path)
    assert result["authoritative_receipt_files"] == 4
    assert result["authoritative_orderfilled_present"] is True
    assert result["settlement_receipt_present"] is False
    assert result["cash_release_checkpoint_present"] is True
    assert result["actual_fills"] == 0
