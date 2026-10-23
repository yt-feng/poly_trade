"""Regression contracts for private paired audits and recoverable evidence."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/offline-paired-cost-audit.yml'


def step(text, marker):
    items = [block for block in re.split(r'(?m)(?=^      - )', text)
             if block.startswith('      - ') and marker in block]
    if len(items) != 1:
        raise AssertionError(marker)
    return items[0]


def script(block):
    match = re.search(r"python - <<'PY'\n(.*?)^          PY\s*$", block, re.M | re.S)
    return textwrap.dedent(match.group(1))


class PairedWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text()

    def test_main_only_offline_inputs_and_private_streams(self):
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
                         {'OFFLINE_PAIRED_COST_CONFIG_B64'})
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API')
        evaluation = step(self.workflow, 'id: evaluate')
        self.assertIn('python analysis/offline_paired_cost_audit.py', evaluation)
        self.assertIn('> "$RUNNER_TEMP/offline-paired-cost-audit/analysis-output.txt" 2>&1', evaluation)
        self.assertNotRegex(evaluation, r'\btee\b|set\s+-x|env:')
        self.assertIn('tests/test_offline_paired_cost_audit.py', step(self.workflow, 'id: contracts'))

    def test_configuration_digest_rejects_unregistered_input_without_disclosure(self):
        raw = json.dumps({'source_commit': 'a' * 40, 'private_value': 'not-public'}).encode()
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', script(step(self.workflow, 'id: configuration'))],
                capture_output=True, text=True, env={**os.environ, 'RUNNER_TEMP': directory,
                'AUDIT_CONFIG_B64': base64.b64encode(raw).decode(), 'EXPECTED_SHA256': '0' * 64,
                'GITHUB_ENV': str(Path(directory) / 'env')})
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('not-public', result.stdout + result.stderr)
            self.assertFalse((Path(directory) / 'offline-paired-cost-audit/config.json').exists())

    def _run_sealing(self, workdir, summary=None, outcome='success', large_proof=False):
        from tools import research_vault
        for relative in ('tools/research_vault.py', 'tools/run_quality_first_research.py', 'analysis/forward_acquire.py'):
            target = workdir / relative
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        key = research_vault.X25519PrivateKey.generate()
        recipient = research_vault.recipient_for(key)
        (workdir / 'research_vault').mkdir()
        (workdir / 'research_vault/recipient.json').write_text(json.dumps(recipient))
        private = workdir / 'runner-temp/offline-paired-cost-audit'
        if summary is not None:
            (private / 'results').mkdir(parents=True)
            (private / 'results/summary.json').write_text(json.dumps(summary))
            (private / 'config.json').write_text('{"private_protocol": true}')
            if large_proof:
                # Exceeds a single plaintext envelope; the established tar/gzip
                # delivery must still retain the complete raw scoring evidence.
                (private / 'results/scores.csv').write_bytes(b'private-score-row\n' * 1_100_000)
        result = subprocess.run([sys.executable, '-c', script(step(self.workflow, 'id: seal'))],
            cwd=workdir, capture_output=True, text=True, env={**os.environ,
                'RUNNER_TEMP': str(workdir / 'runner-temp'), 'QUALITY_FIRST_RECIPIENT_ID': recipient['recipient_id'],
                'EVALUATION_OUTCOME': outcome, 'SETUP_OUTCOMES': '{}', 'REQUEST_SHA256': '0' * 64,
                'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        delivery = workdir / 'sealed-delivery'
        status = json.loads((delivery / 'status.json').read_text())
        parts = [json.loads(research_vault.unseal(path.read_bytes(), key)) for path in sorted(delivery.glob('part-*.vault'))]
        packed = b''.join(base64.b64decode(part['payload_base64']) for part in parts)
        self.assertTrue(all(part['bundle_sha256'] == hashlib.sha256(packed).hexdigest() for part in parts))
        self.assertEqual([part['index'] for part in parts], list(range(len(parts))))
        self.assertEqual(status['sealed_parts'], len(parts))
        with tarfile.open(fileobj=io.BytesIO(packed), mode='r:gz') as archive:
            files = {item.name: archive.extractfile(item).read() for item in archive.getmembers() if item.isfile()}
        self.assertFalse(status['plaintext_published'])
        self.assertFalse(status['live_orders_enabled'])
        self.assertEqual(set(path.name for path in delivery.iterdir()),
                         {'status.json'} | {f'part-{i:04d}.vault' for i in range(len(parts))})
        return status, json.loads(files['run.json']), files

    def test_setup_failure_is_sealed_without_private_input(self):
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), outcome='skipped')
            self.assertFalse(status['strategy_evaluated'])
            self.assertFalse(metadata['causal_comparison_valid'])
            self.assertEqual(metadata['workflow']['EVALUATION_OUTCOME'], 'skipped')
            self.assertEqual(set(files), {'run.json'})

    def test_complete_scores_survive_delivery_above_single_envelope_plaintext_size(self):
        summary = {'strategy_evaluated': True, 'evaluation_status': 'evaluated', 'causal_comparison_valid': True,
                   'fit_records': [{'status': 'fit_with_prior_available_labels', 'scored_rows': 3}]}
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), summary, large_proof=True)
            self.assertTrue(status['strategy_evaluated'])
            self.assertTrue(metadata['causal_comparison_valid'])
            self.assertEqual(files['frozen_input.json'], b'{"private_protocol": true}')
            self.assertEqual(files['results/scores.csv'], b'private-score-row\n' * 1_100_000)

    def test_missing_fit_or_reference_mismatch_cannot_claim_valid_comparison(self):
        fit = {'status': 'fit_with_prior_available_labels', 'scored_rows': 3}
        cases = [
            ({'strategy_evaluated': True, 'evaluation_status': 'evaluated', 'fit_records': []}, False),
            ({'strategy_evaluated': True, 'evaluation_status': 'evaluated_control_mismatch',
              'causal_comparison_valid': False, 'fit_records': [fit]}, True),
        ]
        for summary, evaluated in cases:
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as directory:
                status, metadata, _ = self._run_sealing(Path(directory), summary)
                self.assertIs(status['strategy_evaluated'], evaluated)
                self.assertFalse(metadata['causal_comparison_valid'])

    def test_ciphertext_saved_before_publish_then_plaintext_cleaned(self):
        upload = step(self.workflow, 'actions/upload-artifact@')
        publish = step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertIn('path: sealed-delivery/', upload)
        self.assertNotRegex(upload, r'config\.json|analysis-output|RUNNER_TEMP|source_poly')
        self.assertIn('if: always()', cleanup)
        self.assertIn("'offline-paired-cost-audit'", cleanup)
        contract = (ROOT / '.github/workflows/compound-readiness-contract.yml').read_text()
        self.assertIn('test_offline_paired_workflow.py', contract)


if __name__ == '__main__':
    unittest.main()
