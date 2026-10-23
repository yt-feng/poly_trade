"""Keep private offline audits separate from live access and public artifacts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/offline-policy-audit.yml"


def workflow_steps(text):
    return [block for block in re.split(r"(?m)(?=^      - )", text) if block.startswith("      - ")]


def step_with(text, marker):
    matches = [block for block in workflow_steps(text) if marker in block]
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one step containing {marker}")
    return matches[0]


def inline_python(step):
    match = re.search(r"python - <<'PY'\n(.*?)^          PY\s*$", step, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("Expected one embedded Python program")
    return textwrap.dedent(match.group(1))


class OfflinePolicyWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text()

    def test_registered_config_digest_is_enforced_before_private_file_write(self):
        script = inline_python(step_with(self.workflow, "id: configuration"))
        raw = json.dumps({"source_commit": "a" * 40, "private_marker": "synthetic-private-configuration"}).encode()
        for matches_digest in (True, False):
            with self.subTest(matches_digest=matches_digest), tempfile.TemporaryDirectory() as directory:
                envfile = Path(directory) / "environment"
                result = subprocess.run(
                    [sys.executable, "-c", script], capture_output=True, text=True,
                    env={**os.environ, "RUNNER_TEMP": directory, "GITHUB_ENV": str(envfile),
                         "AUDIT_CONFIG_B64": base64.b64encode(raw).decode(),
                         "EXPECTED_SHA256": hashlib.sha256(raw if matches_digest else b"different").hexdigest()},
                )
                private_file = Path(directory) / "offline-policy-audit/config.json"
                self.assertNotIn("synthetic-private-configuration", result.stdout + result.stderr)
                if matches_digest:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(private_file.read_bytes(), raw)
                    self.assertEqual(private_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(private_file.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(envfile.read_text(), "AUDIT_SOURCE_COMMIT=" + "a" * 40 + "\n")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(private_file.exists())
                    self.assertFalse(envfile.exists())
                    self.assertEqual(result.stderr.strip(), "Private audit configuration missing or invalid")

    def test_invalid_source_ref_cannot_inject_runner_environment(self):
        script = inline_python(step_with(self.workflow, "id: configuration"))
        raw = json.dumps({"source_commit": "main\nUNREGISTERED_SETTING=1"}).encode()
        with tempfile.TemporaryDirectory() as directory:
            envfile = Path(directory) / "environment"
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True,
                env={**os.environ, "RUNNER_TEMP": directory, "GITHUB_ENV": str(envfile),
                     "AUDIT_CONFIG_B64": base64.b64encode(raw).decode(),
                     "EXPECTED_SHA256": hashlib.sha256(raw).hexdigest()},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(envfile.exists())
            self.assertNotIn("UNREGISTERED_SETTING", result.stdout + result.stderr)

    def test_offline_job_has_only_private_audit_config_secret(self):
        self.assertEqual(set(re.findall(r"secrets\.([A-Za-z_][A-Za-z_0-9]*)", self.workflow)),
                         {"OFFLINE_POLICY_AUDIT_CONFIG_B64"})
        self.assertNotRegex(self.workflow, r"secrets\[|toJSON\(secrets\)")
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertNotRegex(self.workflow, r"(?i)\b(?:ssh|scp)\s|PRIVATE_KEY|WALLET|POLYMARKET_API")
        evaluation = step_with(self.workflow, "id: evaluate")
        self.assertIn("python analysis/offline_policy_audit.py", evaluation)
        self.assertIn("--source-root source_poly/data/monthly_runs", evaluation)
        self.assertNotIn("env:", evaluation)

    def test_both_analysis_streams_go_to_private_file_without_public_tee(self):
        evaluation = step_with(self.workflow, "id: evaluate")
        self.assertIn('> "$RUNNER_TEMP/offline-policy-audit/analysis-output.txt" 2>&1', evaluation)
        self.assertNotRegex(evaluation, r"\btee\b|set\s+-x|\bcat\b")
        self.assertIn('--output "$RUNNER_TEMP/offline-policy-audit/results"', evaluation)

    def test_seal_runs_after_skipped_or_failed_evaluation_and_records_setup(self):
        steps = workflow_steps(self.workflow)
        vault_dependencies = step_with(self.workflow, "id: vault_dependencies")
        self.assertIn("python -m pip install -r requirements-vault.txt", vault_dependencies)
        for marker in ("id: configuration", "id: source", "id: dependencies", "id: evaluate"):
            self.assertLess(steps.index(vault_dependencies), steps.index(step_with(self.workflow, marker)))
        sealing = step_with(self.workflow, "id: seal")
        self.assertIn("if: always() && steps.code.outcome == 'success'", sealing)
        self.assertNotRegex(sealing, r"if:.*steps\.(?:evaluate|configuration|source|dependencies|contracts)\.outcome")
        self.assertIn("EVALUATION_OUTCOME: ${{ steps.evaluate.outcome }}", sealing)
        self.assertIn("SETUP_OUTCOMES: ${{ toJSON(steps) }}", sealing)
        self.assertIn("'SETUP_OUTCOMES'", sealing)
        self.assertIn("research_vault.validate_recipient(recipient, recipient_id)", sealing)
        self.assertIn("research_vault.seal(research_vault.canonical(payload), recipient, recipient_id)", sealing)
        self.assertIn("'live_orders_enabled': False", sealing)
        self.assertIn("'actual_fills': 0", sealing)
        self.assertIn("'verified_canary_fills': 0", sealing)

    def test_setup_failure_without_private_directory_still_produces_sealed_outcome(self):
        from tools import research_vault

        script = inline_python(step_with(self.workflow, "id: seal"))
        key = research_vault.X25519PrivateKey.generate()
        recipient = research_vault.recipient_for(key)
        setup = json.dumps({"configuration": {"outcome": "failure"}, "evaluate": {"outcome": "skipped"}})
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            (workdir / "tools").mkdir()
            shutil.copyfile(ROOT / "tools/research_vault.py", workdir / "tools/research_vault.py")
            (workdir / "research_vault").mkdir()
            (workdir / "research_vault/recipient.json").write_text(json.dumps(recipient))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, cwd=directory,
                env={**os.environ, "RUNNER_TEMP": str(workdir / "runner-temp"),
                     "QUALITY_FIRST_RECIPIENT_ID": recipient["recipient_id"],
                     "EVALUATION_OUTCOME": "skipped", "SETUP_OUTCOMES": setup,
                     "REQUEST_SHA256": "0" * 64, "GITHUB_RUN_ID": "1",
                     "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            delivery = workdir / "sealed-delivery"
            self.assertEqual({path.name for path in delivery.iterdir()}, {"part-0000.vault", "status.json"})
            payload = json.loads(research_vault.unseal((delivery / "part-0000.vault").read_bytes(), key))
            self.assertEqual(payload["workflow"]["EVALUATION_OUTCOME"], "skipped")
            self.assertEqual(payload["workflow"]["SETUP_OUTCOMES"], setup)
            self.assertEqual(payload["files"], {})
            self.assertFalse(payload["live_orders_enabled"])
            self.assertEqual(payload["actual_fills"], 0)
            status = json.loads((delivery / "status.json").read_text())
            self.assertFalse(status["strategy_evaluated"])
            self.assertFalse(status["plaintext_published"])

    def test_ciphertext_is_retained_before_publisher_and_alone_in_artifacts(self):
        steps = workflow_steps(self.workflow)
        uploads = [block for block in steps if "actions/upload-artifact@" in block]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        publisher = step_with(self.workflow, "tools/publish_quality_first_journal.py")
        self.assertLess(steps.index(upload), steps.index(publisher))
        self.assertIn("if: always() && steps.seal.outcome == 'success'", upload)
        self.assertIn("if: always() && steps.seal.outcome == 'success'", publisher)
        self.assertEqual(re.findall(r"(?m)^\s+path:\s*(.*)$", upload), ["sealed-delivery/"])
        self.assertIn("if-no-files-found: error", upload)
        self.assertIn('--run-id "$GITHUB_RUN_ID" --run-attempt "$GITHUB_RUN_ATTEMPT"', publisher)
        self.assertIn("--source sealed-delivery", publisher)
        sealing = step_with(self.workflow, "id: seal")
        self.assertEqual(re.findall(r"research_vault\.write_new\(delivery / '([^']+)'", sealing),
                         ["part-0000.vault", "status.json"])
        self.assertIn("'plaintext_published': False", sealing)
        self.assertNotRegex(upload, r"config\.json|analysis-output|results|RUNNER_TEMP|source_poly")

    def test_plaintext_cleanup_is_unconditional_and_after_persistence(self):
        steps = workflow_steps(self.workflow)
        cleanup = step_with(self.workflow, "Remove runner-local plaintext")
        publisher = step_with(self.workflow, "tools/publish_quality_first_journal.py")
        self.assertIn("if: always()", cleanup)
        self.assertGreater(steps.index(cleanup), steps.index(publisher))
        self.assertIn("Path(os.environ['RUNNER_TEMP']) / 'offline-policy-audit'", cleanup)

    def test_public_contract_runs_lifecycle_and_workflow_regressions(self):
        contract = (ROOT / ".github/workflows/compound-readiness-contract.yml").read_text()
        self.assertIn("tests/test_compound_readiness_report.py", contract)
        self.assertIn("test_offline_policy_workflow.py", contract)


if __name__ == "__main__":
    unittest.main()
