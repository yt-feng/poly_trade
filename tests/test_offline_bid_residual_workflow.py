"""Registered forecasting must retain private evidence and honest status."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import test_offline_paired_workflow as shared

ROOT = Path(__file__).resolve().parents[1]


class BidResidualWorkflowTests(unittest.TestCase):
    private_directory = 'offline-bid-residual-audit'
    _run_sealing = shared.PairedWorkflowTests._run_sealing

    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/offline-bid-residual-audit.yml').read_text()

    def test_main_only_private_streams_and_distinct_input(self):
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
                         {'OFFLINE_BID_RESIDUAL_CONFIG_B64'})
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API')
        evaluate = shared.step(self.workflow, 'id: evaluate')
        self.assertIn('analysis/offline_bid_residual_audit.py', evaluate)
        self.assertIn('> "$RUNNER_TEMP/offline-bid-residual-audit/analysis-output.txt" 2>&1', evaluate)
        self.assertNotRegex(evaluate, r'\btee\b|set\s+-x')
        self.assertIn('tests/test_timed_quote_execution.py', shared.step(self.workflow, 'id: contracts'))
        self.assertIn('tests/test_offline_bid_residual_audit.py', shared.step(self.workflow, 'id: contracts'))

    def test_private_input_digest_and_bounds(self):
        raw = json.dumps({'source_commit': 'a' * 40, 'private_value': 'not-public'}).encode()
        for digest, accepted in ((hashlib.sha256(raw).hexdigest(), True), ('0' * 64, False)):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', shared.script(shared.step(self.workflow, 'id: configuration'))],
                    capture_output=True, text=True, env={**os.environ, 'RUNNER_TEMP': directory,
                    'AUDIT_CONFIG_B64': base64.b64encode(raw).decode(), 'EXPECTED_SHA256': digest,
                    'GITHUB_ENV': str(Path(directory) / 'env')})
                self.assertEqual(result.returncode == 0, accepted)
                self.assertNotIn('not-public', result.stdout + result.stderr)
                target = Path(directory) / self.private_directory / 'config.json'
                if accepted:
                    self.assertEqual(target.read_bytes(), raw)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                else:
                    self.assertFalse(target.exists())

    def test_setup_failure_is_sealed_as_not_evaluated(self):
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), outcome='skipped')
            self.assertFalse(status['strategy_evaluated'])
            self.assertFalse(metadata['model_refit'])
            self.assertEqual(set(files), {'run.json'})

    def test_real_fit_and_predictions_count_even_when_policy_does_not_enter(self):
        summary = {'strategy_evaluated': True, 'evaluation_status': 'evaluated',
                   'actual_model_fit_calls': 1, 'actual_model_predict_calls': 1,
                   'model_scored_rows': 3, 'entered_positions': 0,
                   'fit_records': [{'status': 'fit_with_prior_available_labels', 'scored_rows': 3}]}
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), summary)
            self.assertTrue(status['strategy_evaluated'])
            self.assertTrue(metadata['model_refit'])
            self.assertEqual(files['frozen_input.json'], b'{"private_protocol": true}')
            self.assertEqual(metadata['evaluation_class'], 'registered_retrospective_quote_forecast_and_simulation')

    def test_empty_failed_or_unfitted_run_cannot_claim_strategy_evaluation(self):
        base = {'strategy_evaluated': True, 'evaluation_status': 'evaluated',
                'actual_model_fit_calls': 1, 'actual_model_predict_calls': 1,
                'model_scored_rows': 3,
                'fit_records': [{'status': 'fit_with_prior_available_labels', 'scored_rows': 3}]}
        for field, value in [('actual_model_fit_calls', 0), ('actual_model_predict_calls', 0),
                             ('model_scored_rows', 0), ('fit_records', []),
                             ('evaluation_status', 'not_evaluated_insufficient_quote_targets')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                status, metadata, _ = self._run_sealing(Path(directory), {**base, field: value})
                self.assertFalse(status['strategy_evaluated'])
                self.assertEqual(metadata['model_refit'], field != 'actual_model_fit_calls')
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, _ = self._run_sealing(Path(directory), base, outcome='failure')
            self.assertFalse(status['strategy_evaluated'])
            self.assertTrue(metadata['model_refit'])
            self.assertEqual(metadata['model_fit_evidence'], 'summary_fit_counter')
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, _ = self._run_sealing(Path(directory), outcome='failure')
            self.assertFalse(status['strategy_evaluated'])
            self.assertIsNone(metadata['model_refit'])
            self.assertEqual(metadata['model_fit_evidence'], 'fit_counter_unavailable')

    def test_ciphertext_retained_before_persistence_and_plaintext_cleanup(self):
        upload = shared.step(self.workflow, 'actions/upload-artifact@')
        publish = shared.step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = shared.step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertIn('path: sealed-delivery/', upload)
        self.assertNotRegex(upload, r'config\.json|analysis-output|RUNNER_TEMP|source_poly')
        self.assertIn('if: always()', cleanup)
        self.assertIn("'offline-bid-residual-audit'", cleanup)


if __name__ == '__main__':
    unittest.main()
