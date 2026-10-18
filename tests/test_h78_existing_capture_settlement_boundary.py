import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h78_existing_capture_settlement_boundary import audit


def test_report_pnl_is_not_receipt_evidence(tmp_path):
    (tmp_path / "strategy.csv").write_text("strategy,bankroll_after,pnl_usd\na,10,0.1\n", encoding="utf-8")
    result = audit(tmp_path)
    assert result["paper_pnl_present"] is True
    assert result["authenticated_receipt_evidence_present"] is False
    assert result["official_settlement_receipt_present"] is False
    assert result["reusable_cash_clock_present"] is False


def test_receipt_and_cash_keys_are_distinguished(tmp_path):
    (tmp_path / "fixture.json").write_text(
        json.dumps({"owner": "acct", "trade_id": "t1", "transaction_hash": "0xtx", "cash_available_ms": 2}),
        encoding="utf-8",
    )
    result = audit(tmp_path)
    assert result["authenticated_receipt_evidence_present"] is True
    assert result["reusable_cash_clock_present"] is True
    assert result["official_settlement_receipt_present"] is False
