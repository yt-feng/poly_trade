import json
from pathlib import Path

import pandas as pd

from analysis.old_log_prefix_coverage import audit


def _write_log(root: Path, rows: list[dict[str, object]]) -> None:
    path = root / "legacy" / "trade_logs.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _rows() -> list[dict[str, object]]:
    rows = []
    for fold in range(1, 80):
        ts = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15 * fold)
        rows.append(
            {
                "strategy": "candidate",
                "first_quote_ts": ts.isoformat(),
                "run_name": f"run-{fold}",
                "market_id": f"market-{fold}",
                "fold_id": fold,
                "filled_cost": 5.0,
                "fee_usd": 0.0,
                "pnl_usd": 1.0,
            }
        )
    return rows


def test_audit_requires_one_ledger_prefix_and_records_no_live_evidence(tmp_path):
    _write_log(tmp_path, _rows())
    output = tmp_path / "report.json"
    result = audit(tmp_path, output)
    assert result["candidate_records"] == 1
    record = result["records"][0]
    assert record["selection"]["passed"] is True
    assert record["holdout"]["passed"] is True
    assert record["same_ledger_both"] is True
    assert record["promotion_eligible"] is True
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert json.loads(output.read_text())["format"] == "legacy-old-log-prefix-coverage-v1"


def test_missing_fee_is_optimistic_lower_bound_and_not_promotion_eligible(tmp_path):
    rows = _rows()
    for row in rows:
        row.pop("fee_usd")
    _write_log(tmp_path, rows)
    result = audit(tmp_path, tmp_path / "report.json")
    record = result["records"][0]
    assert record["fee_mode"] == "zero_fee_lower_bound"
    assert record["promotion_eligible"] is False


def test_formal_logs_are_excluded_by_default(tmp_path):
    path = tmp_path / "formal" / "trade_logs.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame(_rows()).to_csv(path, index=False)
    result = audit(tmp_path, tmp_path / "report.json")
    assert result["candidate_records"] == 0
