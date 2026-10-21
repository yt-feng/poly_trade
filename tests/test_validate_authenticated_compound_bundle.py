import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
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
    fee = Decimal("0.175")
    balance_before = Decimal("10") + Decimal(i) * Decimal("0.325")
    return {
        "owner": "owner-1",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{i - 1}",
        "status": "confirmed",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "market_id": "market-1",
        "execution_role": "TAKER",
        "shares": "10",
        "price": "0.5",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": format(fee, "f"),
        "fees_enabled": True,
        "fee_taker_only": True,
        "fee_rate": "0.07",
        "fee_exponent": "1",
        "fee_rebate_rate": "0",
        "fee_schedule_market_id": "market-1",
        "fee_schedule_source": "market-snapshot",
        "fee_schedule_observed_ts": str(i * 100 - 1),
        "payout": "5.50",
        "released_cash_since_previous_entry": "5.50" if sequence > 1 else "0",
        "balance_before": format(balance_before, "f"),
        "balance_after": format(balance_before - Decimal("5") - fee, "f"),
    }


def v2_bundle(*, closing_balance: str = "13.25") -> dict[str, object]:
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


def maker_v2_bundle() -> dict[str, object]:
    events = [v2_event(i) for i in range(10)]
    for i, row in enumerate(events):
        row["execution_role"] = "MAKER"
        row["fee_taker_only"] = False
        row["fee_rebate_rate"] = "0.2"
        row["fee"] = "0"
        before = Decimal("10") + Decimal(i) * Decimal("0.5")
        row["balance_before"] = format(before, "f")
        row["balance_after"] = format(before - Decimal("5"), "f")
    bundle = v2_bundle(closing_balance="15")
    bundle["events"] = events
    return bundle


class TestAuthenticatedCompoundBundle(unittest.TestCase):
    def test_legacy_bundle_validator_fails_closed_and_requires_v2(self):
        result = validate_bundle_document(
            {"schema": "authenticated-compound-bundle-v1", "owner": "owner-1", "events": [event(i) for i in range(10)]}
        )
        self.assertEqual(result, {"gate_passed": False, "failure_reason": "legacy_schema_not_admissible_use_v2"})

    def test_bundle_schema_failure_is_explicit(self):
        result = validate_bundle_document({"schema": "wrong", "owner": "owner-1", "events": []})
        self.assertEqual(result, {"gate_passed": False, "failure_reason": "invalid_bundle_schema"})

    def test_cli_returns_nonzero_without_promoting_invalid_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.json"
            bundle.write_text(json.dumps({"schema": "authenticated-compound-bundle-v1", "owner": "owner-1", "events": [event(0)]}))
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "analysis" / "validate_authenticated_compound_bundle.py"), str(bundle)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("gate_passed", proc.stdout)

    def test_v2_bundle_reconciles_one_owner_scoped_compound_chain(self):
        result = validate_bundle_document(v2_bundle())
        self.assertIs(result["gate_passed"], True)
        self.assertEqual(result["fills"], 10)
        self.assertEqual(result["ending_cash"], "13.250")
        self.assertNotIn("ledger", result)

    def test_v2_bundle_fails_closed_on_closing_balance_mismatch(self):
        result = validate_bundle_document(v2_bundle(closing_balance="10.00"))
        self.assertIs(result["gate_passed"], False)
        self.assertEqual(result["failure_reason"], "closing_balance_arithmetic_mismatch")

    def test_v2_bundle_rejects_fee_schedule_observed_after_entry(self):
        document = v2_bundle()
        document["events"][0]["fee_schedule_observed_ts"] = "101"
        result = validate_bundle_document(document)
        self.assertIs(result["gate_passed"], False)
        self.assertEqual(result["failure_reason"], "fee_schedule_observed_after_entry")

    def test_v2_bundle_rejects_fee_amount_not_matching_schedule(self):
        document = v2_bundle()
        document["events"][0]["fee"] = "0.17"
        result = validate_bundle_document(document)
        self.assertIs(result["gate_passed"], False)
        self.assertEqual(result["failure_reason"], "fee_amount_schedule_mismatch")

    def test_v2_bundle_accepts_maker_with_zero_execution_fee_and_rebate_field(self):
        result = validate_bundle_document(maker_v2_bundle())
        self.assertIs(result["gate_passed"], True)
        self.assertEqual(result["fills"], 10)
        self.assertEqual(result["ending_cash"], "15.00")

    def test_v2_bundle_rejects_nonzero_maker_execution_fee(self):
        document = maker_v2_bundle()
        document["events"][0]["fee"] = "0.01"
        result = validate_bundle_document(document)
        self.assertIs(result["gate_passed"], False)
        self.assertEqual(result["failure_reason"], "fee_amount_schedule_mismatch")

    def test_v2_bundle_rejects_non_ten_cli_requirement(self):
        result = validate_bundle_document(v2_bundle(), required_fills=9)
        self.assertEqual(result, {"gate_passed": False, "failure_reason": "required_fills_fixed_at_ten"})
