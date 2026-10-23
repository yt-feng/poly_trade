"""Private bootstrap keys must never enter sealed outputs or public logs."""
import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_offline_paired_workflow as shared

ROOT = Path(__file__).resolve().parents[1]


class ScopedSavedInputWorkflowTests(unittest.TestCase):
    private_directory = 'scoped-saved-input'
    _run_sealing = shared.PairedWorkflowTests._run_sealing

    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/scoped-saved-input-verification.yml').read_text()

    def test_main_only_fixed_secrets_no_archive_or_learner(self):
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
                         {'SCOPED_SAVED_INPUT_CONFIG_B64', 'SCOPED_RESEARCH_DATA_CAPABILITY_B64'})
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API|source_poly|scikit-learn|numpy|pandas')
        evaluate = shared.step(self.workflow, 'id: evaluate')
        self.assertIn('--capability "$RUNNER_TEMP/scoped-saved-input-bootstrap/capability.json"', evaluate)
        self.assertIn('--restored "$RUNNER_TEMP/scoped-saved-input-bootstrap/dataset"', evaluate)
        self.assertIn('> "$RUNNER_TEMP/scoped-saved-input/analysis-output.txt" 2>&1', evaluate)
        self.assertNotRegex(evaluate, r'\btee\b|set\s+-x')

    def materialize(self, directory, raw, capability, *, encoded_config=None, expected_sha=None):
        (directory / 'tools').mkdir()
        shutil.copyfile(ROOT / 'tools/research_vault.py', directory / 'tools/research_vault.py')
        return subprocess.run([sys.executable, '-c', shared.script(shared.step(self.workflow, 'id: configuration'))],
            cwd=directory, capture_output=True, text=True, env={**os.environ, 'RUNNER_TEMP': str(directory),
            'AUDIT_CONFIG_B64': encoded_config or base64.b64encode(lzma.compress(raw)).decode(),
            'INPUT_CAPABILITY_B64': base64.b64encode(json.dumps(capability).encode()).decode(),
            'EXPECTED_SHA256': expected_sha or hashlib.sha256(raw).hexdigest()})

    def test_bootstrap_separate_exact_private_files_and_no_public_output(self):
        from tools import research_vault as vault
        capability = {'version': 1, 'synthetic': 'public-binding', 'content_key_b64': 'synthetic-private-key'}
        descriptor = {k: v for k, v in capability.items() if k != 'content_key_b64'}
        raw = vault.canonical({'descriptor_sha256': hashlib.sha256(vault.canonical(descriptor)).hexdigest()})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.materialize(root, raw, capability)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout + result.stderr, '')
            self.assertEqual((root / 'scoped-saved-input/config.json').read_bytes(), raw)
            cap_path = root / 'scoped-saved-input-bootstrap/capability.json'
            self.assertEqual(json.loads(cap_path.read_text()), capability)
            self.assertEqual(cap_path.stat().st_mode & 0o777, 0o600)
            for name in ['scoped-saved-input', 'scoped-saved-input-bootstrap']:
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o700)
            self.assertNotIn('synthetic-private-key', (root / 'scoped-saved-input/config.json').read_text())
            cleanup = shared.script(shared.step(self.workflow, 'Remove input capability and decoded dataset before sealing'))
            subprocess.run([sys.executable, '-c', cleanup], check=True, env={**os.environ, 'RUNNER_TEMP': str(root)})
            self.assertFalse(cap_path.parent.exists())
            self.assertTrue((root / 'scoped-saved-input/config.json').exists())

    def test_materialization_binding_expansion_trailing_and_missing_caps_fail_without_disclosure(self):
        from tools import research_vault as vault
        capability = {'content_key_b64': 'synthetic-private-key'}
        raw = vault.canonical({'descriptor_sha256': hashlib.sha256(vault.canonical({})).hexdigest()})
        bad_streams = [base64.b64encode(lzma.compress(raw) + b'trailing').decode(),
                       base64.b64encode(lzma.compress(b'x' * 262145)).decode(), 'malformed-base64']
        cases = [(x, None, capability) for x in bad_streams]
        cases += [(None, '0' * 64, capability), (None, None, {**capability, 'wrong': True})]
        for encoded, expected, cap in cases:
            with self.subTest(encoded=encoded is not None, expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = self.materialize(root, raw, cap, encoded_config=encoded, expected_sha=expected)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('synthetic-private-key', result.stdout + result.stderr)
                self.assertFalse((root / 'scoped-saved-input/config.json').exists())

    @staticmethod
    def summary():
        return {'status': 'complete', 'input_verified': True, 'strategy_evaluated': False,
                'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0, 'replayed_cells': 0}

    def test_verified_input_is_never_strategy_or_canary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / 'runner-temp/scoped-saved-input-bootstrap'
            bootstrap.mkdir(parents=True)
            (bootstrap / 'capability.json').write_text('synthetic-private-key')
            status, metadata, files = self._run_sealing(root, self.summary())
            self.assertTrue(metadata['input_verified'])
            self.assertFalse(metadata['strategy_evaluated'])
            self.assertFalse(status['strategy_evaluated'])
            self.assertEqual(metadata['actual_fills'], 0)
            self.assertEqual(metadata['verified_canary_fills'], 0)
            self.assertFalse(any('synthetic-private-key' in value.decode() for value in files.values()))
            self.assertEqual(set(files), {'run.json', 'frozen_input.json', 'results/summary.json'})

    def test_failed_or_nonzero_or_unknown_counters_preserved_never_verified(self):
        for field, value in [('status', 'failed'), ('input_verified', False), ('strategy_evaluated', True),
                             ('actual_model_fit_calls', 2), ('actual_model_fit_calls', None),
                             ('actual_model_predict_calls', 1), ('replayed_cells', 3)]:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                _, meta, _ = self._run_sealing(Path(directory), {**self.summary(), field: value})
                self.assertFalse(meta['input_verified'])
                self.assertFalse(meta['strategy_evaluated'])
                if field in ('actual_model_fit_calls', 'actual_model_predict_calls', 'replayed_cells'):
                    self.assertEqual(meta[field], value)
        for outcome in ('failure', 'skipped'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                _, meta, _ = self._run_sealing(Path(directory), outcome=outcome)
                self.assertFalse(meta['input_verified'])
                self.assertEqual(meta['actual_model_fit_calls'], None if outcome == 'failure' else 0)

    def test_cleanup_before_seal_and_ciphertext_retained_before_publisher(self):
        bootstrap = shared.step(self.workflow, 'Remove input capability and decoded dataset before sealing')
        seal = shared.step(self.workflow, 'id: seal')
        upload = shared.step(self.workflow, 'actions/upload-artifact@')
        publish = shared.step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = shared.step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(bootstrap), self.workflow.index(seal))
        self.assertLess(self.workflow.index(seal), self.workflow.index(upload))
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertIn('if: always()', bootstrap)
        self.assertNotIn('capability', seal)
        self.assertIn('path: sealed-delivery/', upload)
        self.assertNotIn('RUNNER_TEMP', upload)
        self.assertIn('if: always()', cleanup)


if __name__ == '__main__':
    unittest.main()
