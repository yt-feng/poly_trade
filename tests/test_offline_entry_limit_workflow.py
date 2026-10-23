"""Privacy, bounded input and failure-recovery contracts for frozen replays."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import test_offline_paired_workflow as paired_workflow

script, step = paired_workflow.script, paired_workflow.step


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/offline-entry-limit-audit.yml'


class EntryLimitWorkflowTests(unittest.TestCase):
    private_directory = 'offline-entry-limit-audit'
    _run_sealing = paired_workflow.PairedWorkflowTests._run_sealing

    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text()

    def test_offline_private_inputs_and_recoverable_public_outputs(self):
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
                         {'OFFLINE_ENTRY_LIMIT_CONFIG_B64'})
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API')
        evaluate = step(self.workflow, 'id: evaluate')
        self.assertIn('analysis/offline_entry_limit_audit.py', evaluate)
        self.assertIn('> "$RUNNER_TEMP/offline-entry-limit-audit/analysis-output.txt" 2>&1', evaluate)
        self.assertNotRegex(evaluate, r'\btee\b|set\s+-x')
        upload = step(self.workflow, 'actions/upload-artifact@')
        publish = step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertNotRegex(upload, r'config\.json|analysis-output|RUNNER_TEMP|source_poly')
        self.assertIn("'offline-entry-limit-audit'", cleanup)
        self.assertIn('if: always()', cleanup)

    def _materialize(self, packed, digest, directory):
        return subprocess.run([sys.executable, '-c', script(step(self.workflow, 'id: configuration'))],
            capture_output=True, text=True, env={**os.environ, 'RUNNER_TEMP': directory,
            'AUDIT_CONFIG_B64': base64.b64encode(packed).decode(), 'EXPECTED_SHA256': digest,
            'GITHUB_ENV': str(Path(directory) / 'env')})

    def test_registered_gzip_input_round_trips_exact_bytes(self):
        raw = json.dumps({'source_commit': 'a' * 40, 'private_candidates': ['private-value']}).encode()
        with tempfile.TemporaryDirectory() as directory:
            result = self._materialize(gzip.compress(raw), hashlib.sha256(raw).hexdigest(), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            saved = Path(directory) / self.private_directory / 'config.json'
            self.assertEqual(saved.read_bytes(), raw)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('private-value', result.stdout + result.stderr)

    def test_digest_truncation_trailing_members_and_expansion_fail_closed(self):
        raw = json.dumps({'source_commit': 'a' * 40, 'private_value': 'not-public'}).encode()
        packed = gzip.compress(raw)
        cases = [(packed, '0' * 64),
                 (packed[:-1], hashlib.sha256(raw).hexdigest()),
                 (packed + b'junk', hashlib.sha256(raw).hexdigest()),
                 (packed + packed, hashlib.sha256(raw + raw).hexdigest()),
                 (gzip.compress(b'x' * 262145), hashlib.sha256(b'x' * 262145).hexdigest())]
        for payload, digest in cases:
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as directory:
                result = self._materialize(payload, digest, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('not-public', result.stdout + result.stderr)
                self.assertFalse((Path(directory) / self.private_directory / 'config.json').exists())

    def test_setup_failure_is_sealed(self):
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), outcome='skipped')
            self.assertFalse(status['strategy_evaluated'])
            self.assertFalse(metadata['causal_comparison_valid'])
            self.assertEqual(set(files), {'run.json'})

    def test_actual_frozen_replay_does_not_require_new_model_training(self):
        summary = {'strategy_evaluated': True, 'evaluation_status': 'evaluated',
                   'causal_comparison_valid': True, 'candidate_rows_reused': 2,
                   'replayed_cells': 2, 'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0}
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), summary)
            self.assertTrue(status['strategy_evaluated'])
            self.assertTrue(metadata['causal_comparison_valid'])
            self.assertFalse(metadata['model_refit'])
            self.assertEqual(files['frozen_input.json'], b'{"private_protocol": true}')

    def test_empty_replay_and_control_mismatch_are_not_valid_comparisons(self):
        base = {'strategy_evaluated': True, 'evaluation_status': 'evaluated',
                'causal_comparison_valid': True, 'candidate_rows_reused': 2,
                'replayed_cells': 2, 'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0}
        cases = [({**base, 'candidate_rows_reused': 0}, False),
                 ({**base, 'replayed_cells': 0}, False),
                 ({**base, 'actual_model_fit_calls': 1}, False),
                 ({**base, 'evaluation_status': 'evaluated_control_mismatch',
                   'causal_comparison_valid': False}, True)]
        for summary, evaluated in cases:
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as directory:
                status, metadata, _ = self._run_sealing(Path(directory), summary)
                self.assertIs(status['strategy_evaluated'], evaluated)
                self.assertFalse(metadata['causal_comparison_valid'])


if __name__ == '__main__':
    unittest.main()
