import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from validate_authenticated_compound_bundle import validate_bundle_document


def event(i: int) -> dict[str, object]:
    return {
        "owner": "owner-1",
        "status": "confirmed",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
    }


def test_bundle_validator_returns_compact_success_summary():
    result = validate_bundle_document(
        {"schema": "authenticated-compound-bundle-v1", "owner": "owner-1", "events": [event(i) for i in range(10)]}
    )
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert "ledger" not in result
    assert "owner-1" not in json.dumps(result)


def test_bundle_schema_failure_is_explicit():
    result = validate_bundle_document({"schema": "wrong", "owner": "owner-1", "events": []})
    assert result == {"gate_passed": False, "failure_reason": "invalid_bundle_schema"}


def test_cli_returns_nonzero_without_promoting_invalid_bundle(tmp_path: Path):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema": "authenticated-compound-bundle-v1", "owner": "owner-1", "events": [event(0)]}))
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "analysis" / "validate_authenticated_compound_bundle.py"), str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "gate_passed" in proc.stdout
