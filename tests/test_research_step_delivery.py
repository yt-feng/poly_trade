"""Tiny synthetic subprocess and encrypted-outcome checks; no market data."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from tools import research_step_delivery as runner
from tools import research_vault as vault


MARKER = "SYNTHETIC_PRIVATE_CHILD_DIAGNOSTIC"
SOURCE = "a" * 40


@pytest.fixture
def setup(tmp_path, monkeypatch):
    key = vault.X25519PrivateKey.generate()
    recipient = vault.recipient_for(key)
    monkeypatch.setattr(runner, "TRUSTED_RECIPIENT_ID", recipient["recipient_id"])
    recipient_path = tmp_path / "recipient.json"
    recipient_path.write_bytes(vault.canonical(recipient))
    return tmp_path / "private", tmp_path / "delivery", recipient_path, key


def valid_report():
    format_name, hypothesis, fields = runner.PROFILES["h712"]
    return {"format": format_name, "hypothesis_id": hypothesis,
            "source_commit": SOURCE, "observed_source_commit": SOURCE,
            "feature_rows": 1, "live_orders_enabled": False,
            "actual_fills": 0, "verified_canary_fills": 0,
            **{field: {"synthetic": True} for field in fields}}


def execute_fixture(private, recipient_path, *, fail=False, write_report=True):
    code = "import json, pathlib, sys\n"
    code += f"print({MARKER!r} + '_stdout')\nprint({MARKER!r} + '_stderr', file=sys.stderr)\n"
    if write_report:
        code += f"pathlib.Path({str(private / 'results/summary.json')!r}).write_text({json.dumps(valid_report())!r})\n"
    if fail:
        code += f"raise RuntimeError({MARKER!r})\n"
    return runner.execute(private, [sys.executable, "-c", code], timeout_seconds=5, recipient_path=recipient_path)


def recover(target, key):
    parts = [json.loads(vault.unseal(path.read_bytes(), key)) for path in sorted(target.glob("part-*.vault"))]
    raw = b"".join(base64.b64decode(part["payload_base64"]) for part in parts)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}


def test_success_child_and_complete_report_are_sealed_without_public_output(setup, capsys):
    private, target, recipient, key = setup
    assert execute_fixture(private, recipient) == 0
    result = runner.seal(private, target, profile="h712", source_commit=SOURCE,
                         recipient_path=recipient, env={"EVALUATION_OUTCOME": "success"})
    assert result["strategy_evaluated"] is True
    files = recover(target, key)
    assert MARKER.encode() in files["analysis-output.txt"]
    assert json.loads(files["run.json"])["report_contract_valid"] is True
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    assert (private / "analysis-output.txt").stat().st_mode & 0o777 == 0o600


def test_child_error_remains_failure_even_with_valid_partial_report(setup, capsys):
    private, target, recipient, key = setup
    assert execute_fixture(private, recipient, fail=True) == 1
    result = runner.seal(private, target, profile="h712", source_commit=SOURCE,
                         recipient_path=recipient, env={"EVALUATION_OUTCOME": "failure"})
    assert result["delivery_complete"] is True
    assert result["strategy_evaluated"] is False
    files = recover(target, key)
    assert b"RuntimeError" in files["analysis-output.txt"]
    assert MARKER.encode() in files["analysis-output.txt"]
    metadata = json.loads(files["run.json"])
    assert metadata["execution"]["exit_code"] == 1
    assert metadata["evaluation_status"] == "failed_or_not_executed"
    assert MARKER not in capsys.readouterr().out


@pytest.mark.parametrize("report", [None, b"broken JSON", b"{}"])
def test_zero_child_exit_cannot_certify_missing_or_invalid_report(setup, report):
    private, target, recipient, key = setup
    assert execute_fixture(private, recipient, write_report=False) == 0
    if report is not None:
        (private / "results/summary.json").write_bytes(report)
    result = runner.seal(private, target, profile="h712", source_commit=SOURCE,
                         recipient_path=recipient, env={"EVALUATION_OUTCOME": "success"})
    assert result["strategy_evaluated"] is False
    assert json.loads(recover(target, key)["run.json"])["contract_errors"]


def test_setup_failure_is_preserved_without_fabricating_child_execution(setup):
    private, target, recipient, key = setup
    result = runner.seal(private, target, profile="h712", source_commit=SOURCE,
                         recipient_path=recipient, env={"EVALUATION_OUTCOME": "skipped"})
    assert result["strategy_evaluated"] is False
    metadata = json.loads(recover(target, key)["run.json"])
    assert metadata["execution"] == {}
    assert metadata["workflow"]["EVALUATION_OUTCOME"] == "skipped"


def test_duplicate_execution_does_not_launch_another_child(setup, monkeypatch):
    private, _, recipient, _ = setup
    assert execute_fixture(private, recipient) == 0
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate child launched"))
    with pytest.raises(FileExistsError):
        runner.execute(private, ["unused"], timeout_seconds=5, recipient_path=recipient)


def test_recipient_self_consistency_does_not_override_trust_pin(setup, monkeypatch):
    private, _, recipient, _ = setup
    other = vault.recipient_for(vault.X25519PrivateKey.generate())
    recipient.write_bytes(vault.canonical(other))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("untrusted recipient launched child"))
    with pytest.raises(ValueError, match="fingerprint"):
        runner.execute(private, ["unused"], timeout_seconds=5, recipient_path=recipient)
    assert not private.exists()


def test_timeout_terminates_group_and_preserves_timeout_exit(setup, monkeypatch):
    private, target, recipient, key = setup
    class Child:
        pid = 87654321
        returncode = None
        def wait(self, timeout):
            if self.returncode is None:
                self.returncode = -15
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return self.returncode
    kills = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    assert runner.execute(private, ["synthetic"], timeout_seconds=1, recipient_path=recipient) == 124
    assert kills == [(87654321, runner.signal.SIGTERM)]
    runner.seal(private, target, profile="h712", source_commit=SOURCE,
                recipient_path=recipient, env={"EVALUATION_OUTCOME": "failure"})
    assert json.loads(recover(target, key)["run.json"])["execution"]["timed_out"] is True


def test_main_preserves_primary_exit_and_prints_only_generic_status(setup, capsys):
    private, _, recipient, _ = setup
    code = runner.main(["run", "--private", str(private), "--recipient", str(recipient),
                        "--timeout-seconds", "5", "--", sys.executable, "-c",
                        f"import sys; print({MARKER!r}, file=sys.stderr); sys.exit(37)"])
    captured = capsys.readouterr()
    assert code == 37
    assert captured.err == ""
    assert json.loads(captured.out) == {"status": "execution_failed", "plaintext_published": False}


def test_private_contract_and_direct_summary_projection(setup):
    private, target, recipient, key = setup
    private.mkdir()
    format_name, hypothesis, fields = runner.PROFILES["h712"]
    contract = private / "frozen_input.json"
    contract.write_text(json.dumps({"delivery_contract": {
        "expected_format": format_name, "expected_hypothesis": hypothesis,
        "source_commit": SOURCE, "required_report_keys": list(fields),
        "required_object_keys": list(fields),
    }}))
    assert execute_fixture(private, recipient) == 0
    summary = target.parent / "compact-summary"
    result = runner.seal(private, target, contract_path=contract, source_commit=SOURCE,
                         recipient_path=recipient, env={"EVALUATION_OUTCOME": "success"},
                         summary_output=summary)
    assert result["strategy_evaluated"] is True
    projection = json.loads(vault.unseal((summary / "part-0000.vault").read_bytes(), key))
    assert projection["format"] == "private-research-step-summary-v1"
    assert projection["report"] == valid_report()
    assert projection["full_diagnostics_retained"] is True
    assert MARKER in projection["diagnostics"]["analysis-output.txt"]
    assert projection["summary_omissions"] == []
    files = recover(target, key)
    assert json.loads(files["frozen_input.json"])["delivery_contract"]["source_commit"] == SOURCE


def test_direct_summary_does_not_silently_truncate_diagnostics(setup, monkeypatch):
    private, target, recipient, key = setup
    assert execute_fixture(private, recipient) == 0
    monkeypatch.setattr(runner, "MAX_PROJECTION_DIAGNOSTICS_BYTES", 1)
    summary = target.parent / "compact-summary"
    runner.seal(private, target, profile="h712", source_commit=SOURCE,
                recipient_path=recipient, env={"EVALUATION_OUTCOME": "success"}, summary_output=summary)
    projection = json.loads(vault.unseal((summary / "part-0000.vault").read_bytes(), key))
    assert projection["full_diagnostics_retained"] is False
    assert "analysis-output.txt" not in projection["diagnostics"]
    assert projection["summary_omissions"] == [{"member": "analysis-output.txt", "reason": "missing_or_exceeds_compact_budget"}]
    assert MARKER.encode() in recover(target, key)["analysis-output.txt"]


def test_invalid_private_contract_still_seals_failure_evidence(setup):
    private, target, recipient, key = setup
    assert execute_fixture(private, recipient) == 0
    path = private / "frozen_input.json"
    path.write_text(json.dumps({"source_commit": "b" * 40}))
    result = runner.seal(private, target, contract_path=path, source_commit=SOURCE,
                         recipient_path=recipient, env={"EVALUATION_OUTCOME": "success"})
    assert result["strategy_evaluated"] is False
    assert "report_contract_unavailable" in json.loads(recover(target, key)["run.json"])["contract_errors"]


@pytest.mark.parametrize("profile", ["h711", "h712"])
def test_workflows_always_seal_and_retain_ciphertext_before_publishing(profile):
    name = "h711-causal-h600-repair" if profile == "h711" else "h712-causal-h549-repair"
    text = (Path(__file__).parents[1] / ".github/workflows" / (name + ".yml")).read_text()
    assert "id: evaluate" in text and "id: seal" in text
    assert "tools/research_step_delivery.py run" in text
    assert "tools/research_step_delivery.py seal" in text
    assert "if: always() && steps.vault_dependencies.outcome == 'success'" in text
    assert text.count("if: always() && steps.seal.outcome == 'success'") == 3
    assert text.index("uses: actions/upload-artifact@v4") < text.index("tools/publish_quality_first_journal.py")
    assert "sealed-delivery/part-*.vault" in text
    assert "--recipient-id \"$RESEARCH_RECIPIENT_ID\"" in text
    assert runner.TRUSTED_RECIPIENT_ID in text
