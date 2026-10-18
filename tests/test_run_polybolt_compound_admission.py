import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from validate_polybolt_compound_bundle import validate_bundle_document
from test_validate_polybolt_compound_bundle import bundle


TOOL = Path(__file__).resolve().parents[1] / "tools" / "run_polybolt_compound_admission.py"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "polybolt-compound-admission.yml"


def test_tool_redacts_owner_and_ledger_identifiers(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps(bundle()))
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["gate_passed"] is True
    assert "owner" not in result
    assert "ledger_id" not in result
    assert "chain_id" not in result
    assert "ending_cash" not in result


def test_tool_preserves_fail_closed_result(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    document = bundle()
    document["events"][5]["ledger_id"] = "other-ledger"
    source.write_text(json.dumps(document))
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "ledger_id_mismatch"


def test_tool_seals_generic_failure_for_invalid_json(tmp_path):
    source = tmp_path / "bundle.json"
    output = tmp_path / "result.json"
    source.write_text("not-json")
    subprocess.run([sys.executable, str(TOOL), "--bundle", str(source), "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "bundle_invalid"
    assert "not-json" not in output.read_text()


def test_workflow_never_uploads_private_bundle_or_enables_orders():
    text = WORKFLOW.read_text()
    assert "POLYBOLT_COMPOUND_BUNDLE_B64" in text
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
