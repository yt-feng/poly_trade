"""Private cloud comparison bootstrap and failure evidence, using synthetic bytes."""
from __future__ import annotations

import base64
import copy
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
from unittest import mock

import test_offline_paired_workflow as shared
from test_restore_quality_capacity_inputs import make_inputs
from tools import research_vault as vault

ROOT = Path(__file__).resolve().parents[1]


def encoded_xz(raw):
    return base64.b64encode(lzma.compress(raw)).decode()


def padded_json(document, total):
    value = copy.deepcopy(document)
    value['padding'] = ''
    remaining = total - len(vault.canonical(value))
    if remaining < 0:
        raise AssertionError('synthetic document exceeds padding size')
    value['padding'] = 'x'*remaining
    return value


class QualityCapacityWorkflowTests(unittest.TestCase):
    private_directory = 'quality-capacity-audit'

    def _run_sealing(self, workdir, *args, **kwargs):
        tools = workdir / 'tools'
        tools.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / 'tools/seal_research_outcomes.py', tools / 'seal_research_outcomes.py')
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(workdir / 'github-output')}):
            return shared.PairedWorkflowTests._run_sealing(self, workdir, *args, **kwargs)

    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/offline-quality-capacity-audit.yml').read_text()

    def test_main_only_four_scoped_secrets_and_private_streams(self):
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertIn('cancel-in-progress: false', self.workflow)
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
            {'QUALITY_CAPACITY_CONFIG_B64', 'SCOPED_RESEARCH_DATA_CAPABILITY_B64',
             'SCOPED_RESEARCH_CONTROL_CAPABILITY_B64', 'QUALITY_CAPACITY_PRIVATE_AUDIT_B64'})
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API|source_poly|source-root')
        for marker in ('id: inputs', 'id: evaluate', 'id: independent'):
            block = shared.step(self.workflow, marker)
            self.assertIn('analysis-output.txt" 2>&1', block)
            self.assertNotRegex(block, r'\btee\b|set\s+-x')
        self.assertIn('--dataset-root "$RUNNER_TEMP/quality-capacity-bootstrap/dataset"',
                      shared.step(self.workflow, 'id: evaluate'))
        self.assertIn('--control-root "$RUNNER_TEMP/quality-capacity-bootstrap/control"',
                      shared.step(self.workflow, 'id: independent'))
        self.assertIn('--output "$RUNNER_TEMP/quality-capacity-audit/diagnostics.txt"',
                      shared.step(self.workflow, 'id: independent'))
        for package in ('numpy==2.4.6', 'scipy==1.17.1', 'scikit-learn==1.9.1',
                        'pandas==3.1.0', 'joblib==1.6.0', 'threadpoolctl==3.7.0'):
            self.assertIn(package, self.workflow)
        for name in ('test_cached_quote_execution.py', 'test_offline_quality_capacity_audit.py',
                     'test_restore_quality_capacity_inputs.py'):
            self.assertIn(name, shared.step(self.workflow, 'id: contracts'))

    def fixture(self, root):
        config, caps, _, _, _ = make_inputs(root)
        registration = {'format': 'synthetic-preregistration-v1', 'hypotheses': ['fixed-example']}
        script = b'# synthetic standard-library reader\n'
        audit = {'format': 'quality-capacity-private-audit-v1',
            'script_utf8': script.decode('utf-8'), 'registration': registration}
        config['independent_audit'] = {'helper_sha256': hashlib.sha256(script).hexdigest(),
            'registration_canonical_sha256': hashlib.sha256(vault.canonical(registration)).hexdigest()}
        return config, caps, audit

    def materialize(self, root, config, caps, audit, *, raw=None, encoded_config=None,
                    encoded_audit=None, expected_sha=None):
        tools = root / 'tools'; tools.mkdir()
        for name in ('research_vault.py', 'scoped_research_dataset.py'):
            shutil.copyfile(ROOT / 'tools' / name, tools / name)
        raw = vault.canonical(config) if raw is None else raw
        return subprocess.run([sys.executable, '-c', shared.script(shared.step(self.workflow, 'id: configuration'))],
            cwd=root, capture_output=True, text=True, env={**os.environ,
                'RUNNER_TEMP': str(root), 'GITHUB_ENV': str(root / 'github-env'),
                'AUDIT_CONFIG_B64': encoded_xz(raw) if encoded_config is None else encoded_config,
                'DATA_CAPABILITY_B64': base64.b64encode(vault.canonical(caps['dataset'])).decode(),
                'CONTROL_CAPABILITY_B64': base64.b64encode(vault.canonical(caps['control'])).decode(),
                'PRIVATE_AUDIT_B64': encoded_xz(vault.canonical(audit)) if encoded_audit is None else encoded_audit,
                'EXPECTED_SHA256': hashlib.sha256(raw).hexdigest() if expected_sha is None else expected_sha})

    def test_exact_materialization_separates_700_bootstrap_and_600_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, caps, audit = self.fixture(root)
            result = self.materialize(root, config, caps, audit)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout + result.stderr, '')
            private, bootstrap = root / self.private_directory, root / 'quality-capacity-bootstrap'
            self.assertEqual(set(p.name for p in private.iterdir()), {'config.json'})
            self.assertEqual(set(p.name for p in bootstrap.iterdir()),
                             {'dataset-capability.json', 'control-capability.json', 'reader.py', 'registration.json'})
            self.assertEqual((private / 'config.json').read_bytes(), vault.canonical(config))
            self.assertEqual((bootstrap / 'reader.py').read_bytes(), audit['script_utf8'].encode('utf-8'))
            self.assertEqual((bootstrap / 'registration.json').read_bytes(), vault.canonical(audit['registration']))
            for path in (private, bootstrap):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                for member in path.iterdir():
                    self.assertEqual(member.stat().st_mode & 0o777, 0o600)
            for kind in ('dataset', 'control'):
                self.assertEqual(json.loads((bootstrap / (kind + '-capability.json')).read_text()), caps[kind])
                self.assertNotIn(caps[kind]['content_key_b64'], (private / 'config.json').read_text())
            env = (root / 'github-env').read_text()
            self.assertEqual(env, 'PRIVATE_AUDIT_SHA=' + config['independent_audit']['helper_sha256'] + '\n')

    def test_config_and_private_audit_exact_expansion_limits_succeed(self):
        for kind, maximum in (('config', 262144), ('audit', 524288)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, caps, audit = self.fixture(root)
                if kind == 'config':
                    config = padded_json(config, maximum)
                else:
                    # Padding inside registration preserves the audit's exact
                    # schema while reaching its independent expansion cap.
                    audit['registration']['padding'] = ''
                    audit['registration']['padding'] = 'x'*(maximum - len(vault.canonical(audit)))
                    config['independent_audit']['registration_canonical_sha256'] = hashlib.sha256(
                        vault.canonical(audit['registration'])).hexdigest()
                raw = vault.canonical(config)
                self.assertEqual(len(raw if kind == 'config' else vault.canonical(audit)), maximum)
                result = self.materialize(root, config, caps, audit)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_utf8_script_is_hashed_and_materialized_as_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, caps, audit = self.fixture(root)
            audit['script_utf8'] = '# 合成审计示例\n'
            raw_script = audit['script_utf8'].encode('utf-8')
            config['independent_audit']['helper_sha256'] = hashlib.sha256(raw_script).hexdigest()
            result = self.materialize(root, config, caps, audit)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'quality-capacity-bootstrap/reader.py').read_bytes(), raw_script)

    def test_bad_digests_streams_and_expansions_fail_without_public_disclosure(self):
        cases = ('config-digest', 'config-trailing', 'config-concatenated', 'config-expansion',
                 'config-base64', 'config-secret-length', 'audit-secret-length',
                 'audit-trailing', 'audit-expansion', 'script-sha',
                 'registration-sha', 'script-expansion', 'script-nonstring', 'script-surrogate',
                 'script-byte-expansion', 'old-script-field', 'bad-control-capability')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, caps, audit = self.fixture(root)
                kwargs = {}
                raw = vault.canonical(config)
                if case == 'config-digest': kwargs['expected_sha'] = '0'*64
                elif case == 'config-trailing': kwargs['encoded_config'] = base64.b64encode(lzma.compress(raw)+b'trailing').decode()
                elif case == 'config-concatenated': kwargs['encoded_config'] = base64.b64encode(lzma.compress(raw)*2).decode()
                elif case == 'config-expansion': kwargs['encoded_config'] = encoded_xz(b'x'*262145)
                elif case == 'config-base64': kwargs['encoded_config'] = 'invalid-base64'
                elif case == 'config-secret-length': kwargs['encoded_config'] = 'A'*45001
                elif case == 'audit-secret-length': kwargs['encoded_audit'] = 'A'*45001
                elif case == 'audit-trailing': kwargs['encoded_audit'] = base64.b64encode(lzma.compress(vault.canonical(audit))+b'trailing').decode()
                elif case == 'audit-expansion': kwargs['encoded_audit'] = encoded_xz(b'x'*524289)
                elif case == 'script-sha': audit['script_utf8'] = 'changed synthetic script'
                elif case == 'registration-sha': audit['registration']['changed'] = True
                elif case == 'script-expansion':
                    script = b'#'+b'x'*262144
                    audit['script_utf8'] = script.decode('utf-8')
                    config['independent_audit']['helper_sha256'] = hashlib.sha256(script).hexdigest()
                elif case == 'script-nonstring': audit['script_utf8'] = 42
                elif case == 'script-surrogate': audit['script_utf8'] = '\ud800'
                elif case == 'script-byte-expansion':
                    audit['script_utf8'] = '#' + '界'*87382
                    config['independent_audit']['helper_sha256'] = hashlib.sha256(audit['script_utf8'].encode('utf-8')).hexdigest()
                elif case == 'old-script-field':
                    audit['script_base64'] = base64.b64encode(audit.pop('script_utf8').encode('utf-8')).decode()
                elif case == 'bad-control-capability': caps['control']['content_key_b64'] = 'not-a-valid-key'
                result = self.materialize(root, config, caps, audit, **kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('Private comparison configuration missing or invalid', result.stderr)
                self.assertFalse((root / self.private_directory / 'config.json').exists())
                for capability in caps.values():
                    self.assertNotIn(capability['content_key_b64'], result.stdout + result.stderr)

    def test_capability_cleanup_then_bootstrap_cleanup_precede_models_and_sealing(self):
        remove_caps = shared.step(self.workflow, 'Remove capability files before models or independent audit')
        evaluate = shared.step(self.workflow, 'id: evaluate')
        independent = shared.step(self.workflow, 'id: independent')
        remove_bootstrap = shared.step(self.workflow, 'Remove decoded input and private audit bootstrap before sealing')
        seal = shared.step(self.workflow, 'id: seal')
        positions = [self.workflow.index(block) for block in (remove_caps, evaluate, independent, remove_bootstrap, seal)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('if: always()', remove_caps)
        self.assertIn('if: always()', remove_bootstrap)
        self.assertIn("if: always() && steps.evaluate.outcome == 'success'", independent)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bootstrap = root / 'quality-capacity-bootstrap'; bootstrap.mkdir()
            for name in ('dataset-capability.json', 'control-capability.json', 'reader.py', 'registration.json'):
                (bootstrap / name).write_text('synthetic-private-content')
            for name in ('dataset', 'control'):
                (bootstrap / name).mkdir(); (bootstrap / name / 'data.csv').write_text('synthetic-private-table')
            environment = {**os.environ, 'RUNNER_TEMP': str(root)}
            subprocess.run([sys.executable, '-c', shared.script(remove_caps)], check=True, env=environment)
            self.assertFalse((bootstrap / 'dataset-capability.json').exists())
            self.assertFalse((bootstrap / 'control-capability.json').exists())
            self.assertTrue((bootstrap / 'reader.py').exists())
            self.assertTrue((bootstrap / 'dataset/data.csv').exists())
            subprocess.run([sys.executable, '-c', shared.script(remove_bootstrap)], check=True, env=environment)
            self.assertFalse(bootstrap.exists())

    @staticmethod
    def complete_summary():
        return {'evaluation_status': 'evaluated', 'strategy_evaluated': True, 'comparison_valid': True,
            'control_execution_reproduced': True, 'model_scored_rows': 3,
            'actual_model_fit_calls': 2, 'actual_model_predict_calls': 2,
            'price_model_fit_calls': 1, 'capacity_model_fit_calls': 1, 'replayed_cells': 3,
            'control_model_fit_calls': 0, 'control_model_predict_calls': 0}

    def seal(self, root, summary=None, *, outcome='success', independent=None, inputs=None,
             independent_outcome='success', input_outcome='success'):
        private = root / 'runner-temp' / self.private_directory
        private.mkdir(parents=True)
        if independent is not None:
            (private / 'diagnostics.txt').write_text(json.dumps(independent))
        if inputs is not None:
            (private / 'input-receipt.json').write_text(json.dumps(inputs))
        with mock.patch.dict(os.environ, {'INDEPENDENT_OUTCOME': independent_outcome, 'INPUT_OUTCOME': input_outcome,
                                         'PRIVATE_AUDIT_SHA': 'b'*64}):
            return self._run_sealing(root, summary, outcome=outcome)

    def test_all_three_successes_required_and_diagnostics_survive_only_inside_ciphertext(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / 'runner-temp/quality-capacity-bootstrap'; bootstrap.mkdir(parents=True)
            for name in ('dataset-capability.json', 'control-capability.json', 'reader.py', 'data.csv'):
                (bootstrap / name).write_text('synthetic-bootstrap-secret-' + name)
            status, metadata, files = self.seal(root, self.complete_summary(),
                independent={'passed': True, 'synthetic_checks': 3}, inputs={'status': 'complete', 'inputs': {}})
            self.assertTrue(status['strategy_evaluated']); self.assertTrue(metadata['strategy_evaluated'])
            self.assertTrue(metadata['independent_audit_passed']); self.assertTrue(metadata['input_verified'])
            self.assertEqual(metadata['actual_model_fit_calls'], 2)
            self.assertEqual(metadata['control_model_fit_calls'], 0)
            self.assertEqual(set(files), {'run.json', 'frozen_input.json', 'results/summary.json', 'diagnostics.txt'})
            self.assertTrue(json.loads(files['diagnostics.txt'])['passed'])
            self.assertEqual(metadata['independent_diagnostic_sha256'], hashlib.sha256(files['diagnostics.txt']).hexdigest())
            self.assertFalse(metadata['live_orders_enabled']); self.assertEqual(metadata['actual_fills'], 0)
            self.assertEqual(metadata['verified_canary_fills'], 0)
            for content in files.values():
                self.assertNotIn(b'synthetic-bootstrap-secret', content)
            public_status = (root / 'sealed-delivery/status.json').read_text()
            self.assertNotIn('diagnostics', public_status)
            self.assertNotIn('synthetic_checks', public_status)

    def test_seven_failure_modes_remain_sealed_and_never_claim_strategy_success(self):
        complete = self.complete_summary()
        cases = [
            ('setup-not-started', None, 'skipped', None, None, 'skipped', 'skipped', 0),
            ('input-failed', None, 'skipped', None, {'status': 'failed'}, 'skipped', 'failure', 0),
            ('fit-failed', {**complete, 'actual_model_fit_calls': 1, 'actual_model_predict_calls': 0},
             'failure', None, {'status': 'complete'}, 'skipped', 'success', 1),
            ('process-lost-summary', None, 'failure', None, {'status': 'complete'}, 'skipped', 'success', None),
            ('independent-failed', complete, 'success', {'passed': False}, {'status': 'complete'}, 'failure', 'success', 2),
            ('input-receipt-missing', complete, 'success', {'passed': True}, None, 'success', 'success', 2),
            ('nonzero-control', {**complete, 'control_model_fit_calls': 1}, 'success', {'passed': True},
             {'status': 'complete'}, 'success', 'success', 2),
        ]
        for name, summary, outcome, independent, inputs, independent_outcome, input_outcome, fits in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                status, metadata, files = self.seal(Path(directory), summary, outcome=outcome,
                    independent=independent, inputs=inputs, independent_outcome=independent_outcome, input_outcome=input_outcome)
                self.assertFalse(status['strategy_evaluated']); self.assertFalse(metadata['strategy_evaluated'])
                self.assertEqual(metadata['actual_model_fit_calls'], fits)
                self.assertIn('run.json', files)
                self.assertEqual(metadata['model_refit'], fits > 0 if fits is not None else None)

    def test_observed_zero_nonzero_and_invalid_counters_are_not_erased(self):
        for value, expected in ((0, 0), (7, 7), (None, None), (True, None), (-1, None)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                summary = {**self.complete_summary(), 'actual_model_fit_calls': value}
                _, metadata, _ = self.seal(Path(directory), summary, independent={'passed': True}, inputs={'status': 'complete'})
                self.assertEqual(metadata['actual_model_fit_calls'], expected)
                self.assertEqual(metadata['model_refit'], expected > 0 if expected is not None else None)
                if value in (None, True, -1, 0): self.assertFalse(metadata['strategy_evaluated'])
        with tempfile.TemporaryDirectory() as directory:
            _, metadata, _ = self.seal(Path(directory), {**self.complete_summary(), 'actual_model_fit_calls': 9},
                outcome='skipped', independent={'passed': True}, inputs={'status': 'complete'})
            self.assertEqual(metadata['actual_model_fit_calls'], 9)
            self.assertFalse(metadata['strategy_evaluated'])

    def test_ciphertext_retained_before_publisher_then_all_plaintext_cleaned(self):
        upload = shared.step(self.workflow, 'actions/upload-artifact@')
        publish = shared.step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = shared.step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertIn('path: sealed-delivery/', upload)
        self.assertNotRegex(upload, r'RUNNER_TEMP|config\.json|reader\.py|data\.csv|capability')
        self.assertIn('if: always()', cleanup)
        self.assertIn("'quality-capacity-bootstrap'", cleanup)
        self.assertIn("'quality-capacity-audit'", cleanup)
        self.assertNotIn('SCOPED_RESEARCH', shared.step(self.workflow, 'id: seal'))
        for block in (upload, publish):
            self.assertIn("steps.seal.outputs.delivery_complete == 'true'", block)
            self.assertNotIn("steps.seal.outcome == 'success'", block)
        gate = shared.step(self.workflow, 'id: delivery_gate')
        self.assertLess(self.workflow.index(publish), self.workflow.index(gate))
        self.assertLess(self.workflow.index(gate), self.workflow.index(cleanup))
        self.assertIn("os.environ['FULL_EVIDENCE'] != 'true'", gate)
        self.assertIn('max_member_bytes=256 * 1024 ** 2', shared.step(self.workflow, 'id: seal'))
        self.assertIn('test_seal_research_outcomes.py',
            (ROOT / '.github/workflows/compound-readiness-contract.yml').read_text())

    def test_known_size_failure_retains_compact_ciphertext_but_final_gate_fails(self):
        original_script = shared.script
        def scripted(block):
            source = original_script(block)
            if 'id: seal' not in block:
                return source
            return ("import sys\nsys.path.insert(0, 'tools')\n"
                "import run_quality_first_research as injected_bundle\n"
                "original_pack = injected_bundle.make_bundle\npack_calls = 0\n"
                "def first_size_failure(*args, **kwargs):\n"
                "    global pack_calls\n    pack_calls += 1\n"
                "    if pack_calls == 1: raise ValueError('RESULT_SIZE_LIMIT')\n"
                "    return original_pack(*args, **kwargs)\n"
                "injected_bundle.make_bundle = first_size_failure\n" + source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(shared, 'script', side_effect=scripted):
                status, metadata, files = self.seal(root, self.complete_summary(),
                    independent={'passed': True}, inputs={'status': 'complete'})
            self.assertFalse(status['strategy_evaluated'])
            self.assertFalse(metadata['strategy_evaluated']); self.assertFalse(metadata['comparison_valid'])
            self.assertFalse(metadata['full_evidence_retained'])
            self.assertEqual(metadata['analysis_step_outcome'], 'success')
            self.assertEqual(metadata['independent_step_outcome'], 'success')
            self.assertIn('results/summary.json', files); self.assertIn('diagnostics.txt', files)
            self.assertIn('results/delivery-failure.json', files)
            outputs = (root / 'github-output').read_text()
            self.assertEqual(outputs, 'delivery_complete=true\nfull_evidence=false\n')
            gate = original_script(shared.step(self.workflow, 'id: delivery_gate'))
            result = subprocess.run([sys.executable, '-c', gate], capture_output=True, text=True,
                env={**os.environ, 'DELIVERY_COMPLETE': 'true', 'FULL_EVIDENCE': 'false',
                     'RETENTION_OUTCOME': 'success', 'PERSISTENCE_OUTCOME': 'success'})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertIn('Complete research evidence was not retained', result.stderr)


if __name__ == '__main__':
    unittest.main()
