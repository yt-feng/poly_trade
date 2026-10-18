import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h80_existing_capture_evidence_summary import summarize


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_summary_preserves_receipt_boundary_and_paper_metrics(tmp_path):
    write(tmp_path / "current-capture-execution-audit.json", {
        "counts": {"snapshot_rows": 4, "decision_window_rows": 2, "pair_quote_valid": 1, "twap60_valid": 3, "official_threshold_valid": 2},
        "official_outcome_label_rows": 1,
        "label_counts": {"kind:quote_markout_not_fill_pnl": 8},
        "execution_receipts_present": False,
        "execution_receipt_snapshot_rows": 0,
    })
    write(tmp_path / "current-capture-official-outcome.json", {
        "metrics": {"candidate": {"train_selection": {"paper_settlement_trades": 2, "paper_settlement_pnl": "1"}, "test": {"paper_settlement_trades": 1, "paper_settlement_pnl": "-1"}}}
    })
    write(tmp_path / "current-capture-settlement-logistic.json", {
        "eligible_markets_with_complete_features": 2,
        "metrics": {"train_selection": {"paper_settlement_trades": 1, "paper_settlement_pnl": "1"}, "test": {"paper_settlement_trades": 1, "paper_settlement_pnl": "-1"}},
    })
    result = summarize(tmp_path)
    assert result["execution"]["execution_receipts_present"] is False
    assert result["execution"]["quote_markout_label_rows"] == 8
    assert result["official_outcome_candidates"]["candidate"]["test_pnl"] == "-1"
    assert result["actual_fills"] == 0
