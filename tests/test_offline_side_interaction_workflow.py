"""Private paired model audits must keep transport and evidence boundaries."""
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

import test_offline_paired_workflow as shared

ROOT = Path(__file__).resolve().parents[1]


class SideInteractionWorkflowTests(unittest.TestCase):
    private_directory = 'offline-side-interaction-audit'
    _run_sealing = shared.PairedWorkflowTests._run_sealing

    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/offline-side-interaction-audit.yml').read_text()

    def test_main_only_dedicated_input_private_outputs_and_contracts(self):
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
                         {'OFFLINE_SIDE_INTERACTION_CONFIG_B64'})
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API')
        evaluate = shared.step(self.workflow, 'id: evaluate')
        self.assertIn('analysis/offline_side_interaction_audit.py', evaluate)
        self.assertIn('> "$RUNNER_TEMP/offline-side-interaction-audit/analysis-output.txt" 2>&1', evaluate)
        self.assertNotRegex(evaluate, r'\btee\b|set\s+-x')
        contracts = shared.step(self.workflow, 'id: contracts')
        for name in ('test_timed_quote_execution.py', 'test_side_conditioned_ridge.py',
                     'test_offline_side_interaction_audit.py'):
            self.assertIn(name, contracts)

    def run_materialization(self, encoded, digest, directory):
        return subprocess.run([sys.executable, '-c', shared.script(shared.step(self.workflow, 'id: configuration'))],
            capture_output=True, text=True, env={**os.environ, 'RUNNER_TEMP': directory,
            'AUDIT_CONFIG_B64': encoded, 'EXPECTED_SHA256': digest,
            'GITHUB_ENV': str(Path(directory) / 'env')})

    def test_compressed_private_input_roundtrip_exact_digest_and_permissions(self):
        raw = json.dumps({'source_commit': 'a' * 40, 'private_value': 'not-public',
                          'padding': 'x' * 90000}).encode()
        encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_materialization(encoded, hashlib.sha256(raw).hexdigest(), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('not-public', result.stdout + result.stderr)
            target = Path(directory) / self.private_directory / 'config.json'
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_bad_digest_encoding_stream_and_expansion_never_write_input(self):
        raw = json.dumps({'source_commit': 'a' * 40, 'private_value': 'not-public'}).encode()
        packed = gzip.compress(raw, mtime=0)
        digest = hashlib.sha256(raw).hexdigest()
        variants = [('digest', packed, '0' * 64), ('not-gzip', raw, digest),
                    ('truncated', packed[:-1], digest), ('concatenated', packed + packed, digest),
                    ('trailing', packed + b'junk', digest),
                    ('expanded', gzip.compress(b'x' * 262145, mtime=0), digest)]
        for name, payload, expected in variants:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                result = self.run_materialization(base64.b64encode(payload).decode(), expected, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('not-public', result.stdout + result.stderr)
                self.assertFalse((Path(directory) / self.private_directory / 'config.json').exists())
        for encoded in ('***not-base64***', 'x' * 45001):
            with self.subTest(encoded_bytes=len(encoded)), tempfile.TemporaryDirectory() as directory:
                result = self.run_materialization(encoded, digest, directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(directory) / self.private_directory / 'config.json').exists())

    @staticmethod
    def evaluated_summary():
        return {'strategy_evaluated': True, 'evaluation_status': 'evaluated',
                'comparison_valid': True, 'control_execution_reproduced': True,
                'control_model_fit_calls': 0, 'control_model_predict_calls': 0, 'actual_model_fit_calls': 1,
                'actual_model_predict_calls': 1, 'model_scored_rows': 3,
                'fit_records': [{'status': 'fit_with_prior_available_labels', 'scored_rows': 3}]}

    def test_fitted_scored_valid_comparison_is_tested_even_without_entries(self):
        summary = {**self.evaluated_summary(), 'entered_positions': 0}
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), summary)
            self.assertTrue(status['strategy_evaluated'])
            self.assertTrue(metadata['model_refit'])
            self.assertFalse(metadata['control_model_refit'])
            self.assertTrue(metadata['control_execution_reproduced'])
            self.assertTrue(metadata['comparison_valid'])
            self.assertEqual(metadata['evaluation_class'], 'registered_retrospective_paired_model_class_intervention')
            self.assertEqual(metadata['control_prediction_reference'],
                             'cached_audited_parent_metrics_not_refitted_or_fully_rescored_control')
            self.assertEqual(files['frozen_input.json'], b'{"private_protocol": true}')

    def test_invalid_unfitted_or_refitted_control_cannot_claim_valid_strategy_test(self):
        variants = [('comparison_valid', False), ('control_execution_reproduced', False),
                    ('control_model_fit_calls', 1), ('control_model_fit_calls', None),
                    ('control_model_predict_calls', 1), ('control_model_predict_calls', None),
                    ('actual_model_fit_calls', 0), ('actual_model_predict_calls', 0),
                    ('model_scored_rows', 0), ('fit_records', []),
                    ('evaluation_status', 'comparison_invalid')]
        for field, value in variants:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                status, metadata, _ = self._run_sealing(Path(directory), {**self.evaluated_summary(), field: value})
                self.assertFalse(status['strategy_evaluated'])
                self.assertEqual(metadata['model_refit'], field != 'actual_model_fit_calls')
                if field == 'control_model_fit_calls' and value == 1:
                    self.assertTrue(metadata['control_model_refit'])

    def test_setup_and_failed_model_evidence_are_distinct_and_sealed(self):
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), outcome='skipped')
            self.assertFalse(status['strategy_evaluated'])
            self.assertFalse(metadata['model_refit'])
            self.assertFalse(metadata['control_model_refit'])
            self.assertEqual(set(files), {'run.json'})
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, _ = self._run_sealing(Path(directory), outcome='failure')
            self.assertFalse(status['strategy_evaluated'])
            self.assertIsNone(metadata['model_refit'])
            self.assertIsNone(metadata['control_model_refit'])
            self.assertEqual(metadata['model_fit_evidence'], 'fit_counter_unavailable')
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, _ = self._run_sealing(Path(directory), self.evaluated_summary(), outcome='failure')
            self.assertFalse(status['strategy_evaluated'])
            self.assertTrue(metadata['model_refit'])
            self.assertFalse(metadata['control_model_refit'])

    def test_ciphertext_retained_before_publisher_and_plaintext_always_cleaned(self):
        upload = shared.step(self.workflow, 'actions/upload-artifact@')
        publish = shared.step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = shared.step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertIn('path: sealed-delivery/', upload)
        self.assertNotRegex(upload, r'config\.json|analysis-output|RUNNER_TEMP|source_poly')
        self.assertIn('if: always()', cleanup)
        self.assertIn("'offline-side-interaction-audit'", cleanup)


if __name__ == '__main__':
    unittest.main()
