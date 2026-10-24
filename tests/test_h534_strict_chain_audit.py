import json
from pathlib import Path

import pytest

from h534_strict_chain_audit import audit


def _summary(tmp_path: Path, *, passing=True, duplicate=False):
    rows = []
    for stress in ((0.005, 2.0, 0.10), (0.01, 8.0, 0.25)):
        for window in range(1, 18):
            rows.append({
                "slippage": stress[0],
                "latency_seconds": stress[1],
                "depth_haircut": stress[2],
                "gross_cash_fraction": 0.55,
                "window_index": window,
                "first_ten_fills": 10 if passing else 9,
                "first_ten_passed": passing,
                "first_ten_min_cash_before_entry": 5.0,
                "first_ten_ending_cash": 11.0 if passing else 4.0,
            })
    if duplicate:
        rows.append(dict(rows[0]))
    value = {
        "format": "old-clean-h534-dense-cash-fraction-grid-v1",
        "hypothesis_id": "H534_DENSE_FIXED_CASH_FRACTION_GRID",
        "source_commit": "abc",
        "observed_source_commit": "abc",
        "protocol": {
            "initial_cash_usd": 10.0,
            "required_fills": 10,
            "minimum_order_usd": 5.0,
            "same_ledger_within_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "window_rows": rows,
    }
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(value))
    return path


def test_audit_counts_fixed_fraction_rows_and_separates_fills(tmp_path):
    out = tmp_path / "audit.json"
    result = audit(_summary(tmp_path), source_commit="abc", output=out)
    assert result["same_ledger_compounding_rows"] == 34
    assert result["selection"]["all_ten"] is True
    assert result["holdout"]["all_ten"] is True
    assert result["strict_chain_gate"] is True
    assert result["actual_fills"] == result["verified_canary_fills"] == 0


def test_audit_does_not_turn_failed_rows_into_gate(tmp_path):
    result = audit(_summary(tmp_path, passing=False), source_commit="abc", output=tmp_path / "audit.json")
    assert result["same_ledger_compounding_rows"] == 0
    assert result["strict_chain_gate"] is False


def test_audit_rejects_duplicate_fixed_grid_identity(tmp_path):
    with pytest.raises(ValueError, match="DUPLICATE_FIXED_FRACTION_ROW"):
        audit(_summary(tmp_path, duplicate=True), source_commit="abc", output=tmp_path / "audit.json")
