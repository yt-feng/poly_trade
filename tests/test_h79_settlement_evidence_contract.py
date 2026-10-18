import copy
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


def bundle(events):
    return {"schema": "authenticated-compound-bundle-v1", "owner": "owner-1", "events": events}


def test_legacy_bundle_is_never_admitted_even_with_ten_rows():
    result = validate_bundle_document(bundle([event(i) for i in range(10)]))
    assert result == {"gate_passed": False, "failure_reason": "legacy_schema_not_admissible_use_v2"}


def test_missing_settlement_receipt_fails_closed():
    row = event(0)
    row.pop("settlement_receipt_id")
    result = validate_bundle_document(bundle([row]))
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "legacy_schema_not_admissible_use_v2"


def test_unreconciled_cash_fails_closed_even_with_confirmed_trade():
    row = event(0)
    row["cash_reconciled"] = False
    result = validate_bundle_document(bundle([row]))
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "legacy_schema_not_admissible_use_v2"


def test_five_fills_cannot_be_combined_into_a_ten_fill_gate():
    result = validate_bundle_document(bundle([event(i) for i in range(5)]))
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "legacy_schema_not_admissible_use_v2"
