import base64
import hashlib
import json
import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from validate_polybolt_compound_bundle import validate_bundle_document
from validate_polybolt_compound_bundle import H530_STRATEGY_CONFIG_SHA256, H530_STRATEGY_ID, H530_STRATEGY_PROFILE, H530_STRATEGY_VERSION
from validate_polybolt_compound_bundle import H546_STRATEGY_CONFIG_SHA256, H546_STRATEGY_ID, H546_STRATEGY_PROFILE, H546_STRATEGY_VERSION
from validate_polybolt_compound_bundle import H549_STRATEGY_CONFIG_SHA256, H549_STRATEGY_ID, H549_STRATEGY_PROFILE, H549_STRATEGY_VERSION
from validate_polybolt_compound_bundle import H561_STRATEGY_CONFIG_SHA256, H561_STRATEGY_ID, H561_STRATEGY_PROFILE, H561_STRATEGY_VERSION, _expected_entry_terms
from validate_polybolt_compound_bundle import H578_STRATEGY_CONFIG_SHA256, H578_STRATEGY_ID, H578_STRATEGY_PROFILE, H578_STRATEGY_VERSION
from validate_polybolt_compound_bundle import PRIVATE_STRATEGY_PROFILE
from test_validate_polybolt_compound_bundle import (
    attach_strategy_trace,
    assert_unverified_receipts,
    bundle,
    configure_adaptive_fixture,
)
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
    configure_adaptive_fixture(document, ["0.5"] * 10)
    attach_strategy_trace(document, ["0.5"] * 10, track_profits=False)
    source.write_text(json.dumps(document))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", H530_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == H530_STRATEGY_PROFILE


def test_tool_accepts_the_frozen_h546_adaptive_profile(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["strategy"] = {
        "id": H546_STRATEGY_ID,
        "version": H546_STRATEGY_VERSION,
        "config_sha256": H546_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(document, ["0.55"] + ["0.575"] * 9)
    attach_strategy_trace(document, ["0.55"] + ["0.575"] * 9)
    source.write_text(json.dumps(document))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", H546_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == H546_STRATEGY_PROFILE


def test_tool_accepts_the_frozen_h549_adaptive_profile(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["strategy"] = {
        "id": H549_STRATEGY_ID,
        "version": H549_STRATEGY_VERSION,
        "config_sha256": H549_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(document, ["0.55"] + ["0.5875"] * 9)
    attach_strategy_trace(document, ["0.55"] + ["0.5875"] * 9)
    source.write_text(json.dumps(document))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", H549_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == H549_STRATEGY_PROFILE


def test_tool_accepts_the_frozen_h561_depth_proxy_without_private_config(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["strategy"] = {
        "id": H561_STRATEGY_ID,
        "version": H561_STRATEGY_VERSION,
        "config_sha256": H561_STRATEGY_CONFIG_SHA256,
    }
    # Build a full-cash H561 chain and attach the depth trace so this test
    # covers the cloud runner's built-in profile path without a private
    # strategy secret.
    cash = Decimal("10")
    previous_payout = Decimal("0")
    for event, receipt, settlement in zip(
        document["events"], document["cash_release_receipts"], document["settlement_receipts"], strict=True
    ):
        shares, cost, _expected_fee = _expected_entry_terms(cash, Decimal("0.5"), Decimal("1"))
        # Keep the synthetic balance checkpoint exactly nonnegative; the
        # validator accepts this within its fee arithmetic tolerance.
        fee = cash - cost
        payout = cost + fee + Decimal("0.50")
        event.update({
            "shares": format(shares, "f"), "price": "0.5", "entry_cost": format(cost, "f"),
            "fee": format(fee, "f"), "payout": format(payout, "f"),
            "released_cash_since_previous_entry": format(previous_payout, "f"),
            "balance_before": format(cash, "f"),
            "balance_after": format(cash - cost - fee, "f"),
        })
        receipt["amount"] = format(payout, "f")
        settlement["payout"] = format(payout, "f")
        previous_payout = payout
        cash = cash - cost - fee + payout
    document["closing_balance"] = format(cash, "f")
    from test_validate_polybolt_compound_bundle import attach_depth_proxy_trace
    attach_depth_proxy_trace(document)
    source.write_text(json.dumps(document))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", H561_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == H561_STRATEGY_PROFILE


def test_tool_accepts_the_separate_h578_depth_proxy_without_private_config(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["strategy"] = {
        "id": H578_STRATEGY_ID,
        "version": H578_STRATEGY_VERSION,
        "config_sha256": H578_STRATEGY_CONFIG_SHA256,
    }
    from test_validate_polybolt_compound_bundle import attach_depth_proxy_trace
    cash = Decimal("10")
    previous_payout = Decimal("0")
    for event, receipt, settlement in zip(
        document["events"], document["cash_release_receipts"], document["settlement_receipts"], strict=True
    ):
        shares, cost, _expected_fee = _expected_entry_terms(cash, Decimal("0.5"), Decimal("1"))
        fee = cash - cost
        payout = cost + fee + Decimal("0.50")
        event.update({
            "shares": format(shares, "f"), "price": "0.5", "entry_cost": format(cost, "f"),
            "fee": format(fee, "f"), "payout": format(payout, "f"),
            "released_cash_since_previous_entry": format(previous_payout, "f"),
            "balance_before": format(cash, "f"),
            "balance_after": format(cash - cost - fee, "f"),
        })
        receipt["amount"] = format(payout, "f")
        settlement["payout"] = format(payout, "f")
        previous_payout = payout
        cash = cash - cost - fee + payout
    document["closing_balance"] = format(cash, "f")
    attach_depth_proxy_trace(document)
    source.write_text(json.dumps(document))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", H578_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == H578_STRATEGY_PROFILE


def test_tool_accepts_private_exact_profile_from_runner_local_config(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    config_path = tmp_path / "strategy-profile.json"
    private = {
        "id": "opaque-private-candidate",
        "version": "opaque-private-v1",
        "config": {"private_parameter": "opaque", "required_fills": 10},
    }
    digest = hashlib.sha256(json.dumps(private["config"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    document = bundle()
    document["strategy"] = {"id": private["id"], "version": private["version"], "config_sha256": digest}
    source.write_text(json.dumps(document))
    config_path.write_text(json.dumps(private))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", PRIVATE_STRATEGY_PROFILE,
        "--private-strategy-config", str(config_path),
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is True
    assert result["strategy_profile"] == PRIVATE_STRATEGY_PROFILE


def test_tool_private_exact_profile_without_config_fails_closed(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps(bundle()))
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", PRIVATE_STRATEGY_PROFILE,
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "private_strategy_profile_unavailable"


def test_tool_private_exact_profile_missing_config_file_is_explicit(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps(bundle()))
    missing = tmp_path / "missing-profile.json"
    subprocess.run([
        sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output),
        "--strategy-profile", PRIVATE_STRATEGY_PROFILE,
        "--private-strategy-config", str(missing),
    ], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["offline_consistency_passed"] is False
    assert result["failure_reason"] == "private_strategy_profile_unavailable"


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
    assert "default: h578_online_depth_proxy" in text
    assert "inputs.strategy_profile || 'h578_online_depth_proxy'" in text
    assert "h530_fixed_half_cash" in text
    assert "h546_adaptive_one_profit" in text
    assert "h549_adaptive_one_profit" in text
    assert "h561_online_depth_proxy" in text
    assert "h578_online_depth_proxy" in text
    assert "h30_legacy" in text
    assert "private_exact" in text
    assert "--strategy-profile" in text
    assert "--private-strategy-config" in text
    assert "POLYBOLT_STRATEGY_PROFILE_JSON_B64" in text
    assert "STRATEGY_PROFILE" in text
    assert "POLYBOLT_COMPOUND_BUNDLE_B64" in text
    assert "Detect private bundle without exposing its contents" in text
    assert "id: bundle_preflight" in text
    assert "steps.bundle_preflight.outputs.available == 'true'" in text
    assert "Write fail-closed result when bundle unavailable" in text
    assert "bundle_secret_unavailable" in text
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
