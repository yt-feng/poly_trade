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
        "cash_release_receipt_id": f"cash-release-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
    }


def v2_event(i: int) -> dict[str, object]:
    sequence = i + 1
    balance_before = 10 + i * 0.5
    return {
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{i - 1}",
        "status": "confirmed",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
        "released_cash_since_previous_entry": "5.50" if sequence > 1 else "0",
        "balance_before": str(balance_before),
        "balance_after": str(balance_before - 5),
    }


def v2_bundle(*, closing_balance: str = "15.00") -> dict[str, object]:
    return {
        "schema": "authenticated-compound-bundle-v2",
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "starting_checkpoint_id": "opening",
        "closing_balance_checkpoint_id": "closing",
        "closing_balance": closing_balance,
        "events": [v2_event(i) for i in range(10)],
    }


def test_legacy_bundle_validator_fails_closed_and_requires_v2():
    result = validate_bundle_document(
        {"schema": "authenticated-compound-bundle-v1", "owner": "owner-1", "events": [event(i) for i in range(10)]}
    )
    assert result == {"gate_passed": False, "failure_reason": "legacy_schema_not_admissible_use_v2"}


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


def test_v2_bundle_reconciles_one_owner_scoped_compound_chain():
    result = validate_bundle_document(v2_bundle())
    assert result["gate_passed"] is True
    assert result["fills"] == 10
    assert result["ending_cash"] == "15.00"
    assert "ledger" not in result


def test_v2_bundle_fails_closed_on_closing_balance_mismatch():
    result = validate_bundle_document(v2_bundle(closing_balance="10.00"))
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "closing_balance_arithmetic_mismatch"


def test_v2_bundle_rejects_non_ten_cli_requirement():
    result = validate_bundle_document(v2_bundle(), required_fills=9)
    assert result == {"gate_passed": False, "failure_reason": "required_fills_fixed_at_ten"}
