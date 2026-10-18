import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from authoritative_orderfilled_source_audit import audit


def test_quote_only_sources_are_not_orderfilled_ready(tmp_path):
    old = tmp_path / "old"
    current = tmp_path / "current"
    old.mkdir()
    current.mkdir()
    (old / "quotes.csv").write_text(
        "ts_iso,slug,buy_up_cents,trade_count_1s,trade_volume_1s\n"
        "1,a,50,,\n",
        encoding="utf-8",
    )
    (current / "audit.json").write_text(
        json.dumps({"execution_receipts_present": False, "official_outcome_label_rows": 3}),
        encoding="utf-8",
    )
    result = audit(old, current)
    assert result["old_clean"]["csv_files"] == 1
    assert result["old_clean"]["required_field_presence"]["trade_count_1s"] is True
    assert result["old_clean"]["required_field_presence"]["trade_id"] is False
    assert result["orderfilled_join_ready"] is False
    assert result["execution_receipts_verified"] is False
    assert result["verified_canary_fills"] == 0


def test_authenticated_receipt_field_is_detected_but_not_invented(tmp_path):
    old = tmp_path / "old"
    current = tmp_path / "current"
    old.mkdir()
    current.mkdir()
    (old / "quotes.csv").write_text("slug,buy_up_cents\na,50\n", encoding="utf-8")
    (current / "receipt.json").write_text(
        json.dumps({"trade_id": "t1", "transaction_hash": "0x1", "execution_receipts_present": True}),
        encoding="utf-8",
    )
    result = audit(old, current)
    assert result["orderfilled_join_ready"] is True
    assert result["execution_receipts_verified"] is True
    assert result["actual_fills"] == 0

