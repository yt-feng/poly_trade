import base64
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from validate_polybolt_compound_bundle import validate_bundle_document
from validate_polybolt_compound_bundle import H530_STRATEGY_CONFIG_SHA256, H530_STRATEGY_ID, H530_STRATEGY_PROFILE, H530_STRATEGY_VERSION
from test_validate_polybolt_compound_bundle import assert_unverified_receipts, bundle
import research_vault
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


TOOL = Path(__file__).resolve().parents[1] / "tools" / "run_polybolt_compound_admission.py"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "polybolt-compound-admission.yml"


def test_tool_redacts_owner_and_ledger_identifiers(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps(bundle()))
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert_unverified_receipts(result)
    assert "owner" not in result
    assert "ledger_id" not in result
    assert "chain_id" not in result
    assert "ending_cash" not in result


def test_tool_accepts_the_fixed_half_cash_profile_only_when_explicit(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["strategy"] = {
        "id": H530_STRATEGY_ID,
        "version": H530_STRATEGY_VERSION,
        "config_sha256": H530_STRATEGY_CONFIG_SHA256,
    }
    source.write_text(json.dumps(document))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", H530_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == H530_STRATEGY_PROFILE


def test_tool_preserves_fail_closed_result(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["events"][5]["ledger_id"] = "other-ledger"
    source.write_text(json.dumps(document))
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is False
    assert_unverified_receipts(result)
    assert result["failure_reason"] == "ledger_id_mismatch"


def test_tool_records_missing_bundle_without_fabricating_evidence(tmp_path):
    source = tmp_path / "missing-bundle.json"
    output = tmp_path / "result.json"
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "bundle_secret_unavailable"
    assert_unverified_receipts(result)


def test_tool_seals_generic_failure_for_invalid_json(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    source.write_text("not-json")
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is False
    assert_unverified_receipts(result)
    assert result["failure_reason"] == "bundle_invalid"
    assert "not-json" not in output.read_text()


def test_workflow_never_uploads_private_bundle_or_enables_orders():
    text = WORKFLOW.read_text()
    assert "strategy_profile" in text
    assert "h530_fixed_half_cash" in text
    assert "--strategy-profile" in text
    assert "POLYBOLT_COMPOUND_BUNDLE_B64" in text
    assert "redacted fail-closed result" in text
    materialize = text.split("name: Materialize private bundle only in runner temp", 1)[1].split(
        "name: Run v2 offline consistency checks", 1
    )[0]
    assert "raise SystemExit(0)" not in materialize
    assert "if not value:" in materialize
    assert "private bundle secret is unavailable" not in text
    assert "tests/test_polymarket_receipt_adapter.py" in text
    assert "tests/test_polymarket_user_stream_adapter.py" in text
    assert "result.get('consecutive') is True" in text
    assert "result.get('same_ledger_identity_verified') is True" in text
    assert "result.get('cash_amounts_reconciled') is True" in text
    assert "result.get('source_fill_references_verified') is True" in text
    assert "actions/upload-artifact" not in text
    assert "live_orders_enabled': False" in text
    assert "actual_fills': 0" in text
    assert "POLYBOLT_COMPOUND_BUNDLE_B64" not in text.split("name: Publish only encrypted admission result", 1)[1]


@pytest.mark.parametrize("valid", [True, False])
def test_validator_cli_exit_code_describes_consistency_not_authentication(tmp_path, valid):
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps(bundle() if valid else {}))
    validator = TOOL.parents[1] / "analysis" / "validate_polybolt_compound_bundle.py"
    completed = subprocess.run([sys.executable, str(validator), str(source)], capture_output=True, text=True)
    assert completed.returncode == (0 if valid else 1)
    result = json.loads(completed.stdout)
    assert result["offline_consistency_passed"] is valid
    assert_unverified_receipts(result)


def seal_workflow_result(tmp_path, monkeypatch, result):
    # Execute the exact checked-in sealing Python locally, without invoking
    # Actions, materializing secrets, publishing, or calling external services.
    text = WORKFLOW.read_text()
    section = text.split("      - name: Seal redacted admission result\n", 1)[1].split(
        "      - name: Publish only encrypted admission result", 1)[0]
    script = textwrap.dedent(section.split("<<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0])
    private = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    recipient = research_vault.recipient_for(private)
    (tmp_path / "result.json").write_text(json.dumps(result))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["synthetic-workflow", str(tmp_path)])
    for key, value in {
        "QUALITY_FIRST_RECIPIENT_JSON_B64": base64.b64encode(research_vault.canonical(recipient)).decode(),
        "QUALITY_FIRST_RECIPIENT_ID": recipient["recipient_id"],
        "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REF": "refs/heads/main",
    }.items():
        monkeypatch.setenv(key, value)
    exec(compile(script, "<synthetic-workflow-sealing>", "exec"), {})
    return json.loads(research_vault.unseal((tmp_path / "sealed-delivery/part-0000.vault").read_bytes(), private))


@pytest.mark.parametrize("case", ["consistent", "incomplete", "invalid_json"])
def test_runner_to_workflow_never_promotes_successful_exit(tmp_path, monkeypatch, case):
    doc = bundle()
    if case == "incomplete":
        doc["events"].pop()
    source, output = tmp_path / "bundle.json", tmp_path / "result.json"
    source.write_text("not-json" if case == "invalid_json" else json.dumps(doc))
    completed = subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)],
                               capture_output=True, text=True)
    # The runner writes diagnostic records even for invalid input.
    assert completed.returncode == 0
    result = json.loads(output.read_text())
    assert_unverified_receipts(result)
    public = json.loads(completed.stdout)
    assert public["gate_passed"] is False
    assert public["verified_canary_fills"] == 0
    assert public["offline_consistency_passed"] is (case == "consistent")
    payload = seal_workflow_result(tmp_path, monkeypatch, result)
    assert_unverified_receipts(payload)
    assert payload["format"] == "polybolt-compound-offline-consistency-sealed-v1"
    assert_unverified_receipts(payload["result"])
    assert payload["offline_consistency_passed"] is (case == "consistent")
    assert payload["offline_consistent_compounding_rows"] == (10 if case == "consistent" else 0)
    assert payload["same_ledger_compounding_rows"] == 0
    assert payload["live_orders_enabled"] is False


@pytest.mark.parametrize("case", ["legacy_success", "self_attested", "missing_evidence"])
def test_workflow_never_trusts_claimed_authentication_or_legacy_gate(tmp_path, monkeypatch, case):
    result = validate_bundle_document(bundle())
    result.update(gate_passed=True, receipt_authenticity_verified=True, verified_canary_fills=10, actual_fills=10)
    if case == "legacy_success":
        del result["offline_consistency_passed"]
    elif case == "missing_evidence":
        del result["source_fill_references_verified"]
    payload = seal_workflow_result(tmp_path, monkeypatch, result)
    assert_unverified_receipts(payload)
    assert_unverified_receipts(payload["result"])
    assert payload["same_ledger_compounding_rows"] == 0
    assert payload["offline_consistency_passed"] is (case == "self_attested")
    assert payload["offline_consistent_compounding_rows"] == (10 if case == "self_attested" else 0)
