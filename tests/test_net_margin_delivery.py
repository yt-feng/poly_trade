"""Tiny paired learned-gate delivery; no historical inputs, fits or network."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import prepare_exit_retry_inputs as common
import prepare_net_margin_inputs as prepare
import restore_quality_capacity_inputs as restore
import research_vault as vault
import seal_exit_retry_outcomes as sealer
import persist_quality_first_journal as journal

spec = importlib.util.spec_from_file_location('net_margin_transport_fixtures', Path(__file__).with_name('test_restore_quality_capacity_inputs.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class NetMarginStateTests(unittest.TestCase):
    def setUp(self):
        self.config = {'evaluation_mode': 'offline_net_margin',
            'parent_execution_config': {'parent_strategy_config': {'expected_control_candidate_count': 3}},
            'training_binding': {'expected_test_cohort': {'scored_rows': 8}},
            'max_model_fit_calls': 2, 'max_model_predict_calls': 2}
        ledger = {'scope': 'pooled_single_ledger', 'block_index': 0}
        self.summary = {'strategy_evaluated': True, 'comparison_valid': True, 'evaluation_status': 'evaluated',
            'baseline_control_reproduced': True, 'prefix_control_reproduced': True, 'control_execution_reproduced': True,
            'candidate_rows': 3, 'actual_model_fit_calls': 2, 'actual_model_predict_calls': 2,
            'net_margin_model_fit_calls': 2, 'net_margin_model_predict_calls': 2, 'model_tree_reconstructions': 2,
            'control_model_fit_calls': 0, 'control_model_predict_calls': 0, 'capacity_model_fit_calls': 0,
            'capacity_model_predict_calls': 0, 'model_scored_rows': 8, 'model_forecast_rows': 6, 'gate_rows': 3,
            'replayed_cells': 2, 'producer_replayed_cells': 2, 'producer_replay_completions': 2,
            'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False,
            'arms': {'C_scheduled_retry_fullstream': {**ledger, 'ending_cash': 3},
                'C_net_margin_veto': {**ledger, 'fills': 1, 'ending_cash': 2, 'locked_shares': 5}}}
        self.independent = {'format': 'independent-net-margin-reader-v1', 'passed': True, 'independent_ledger_reconstructions': 2}

    def state(self, summary=None, **kwargs):
        return sealer.evaluation_state(self.config, self.summary if summary is None else summary,
            kwargs.get('independent', self.independent), kwargs.get('inputs', {'status': 'complete'}),
            kwargs.get('evaluation_outcome', 'success'), kwargs.get('independent_outcome', 'success'),
            kwargs.get('input_outcome', 'success'), kwargs.get('scoped_inputs', {'status': 'complete'}))

    def test_negative_or_short_or_locked_treatment_is_valid_technical_evidence(self):
        state = self.state()
        self.assertTrue(state['strategy_evaluated'])
        self.assertEqual(state['actual_model_fit_calls'], 2)
        self.assertEqual(state['model_scored_rows'], 8)
        self.assertEqual(state['gate_rows'], 3)

    def test_zero_eligible_and_zero_trades_are_valid_no_backfill(self):
        summary = copy.deepcopy(self.summary)
        for key in ('actual_model_fit_calls', 'actual_model_predict_calls', 'net_margin_model_fit_calls',
                    'net_margin_model_predict_calls', 'model_forecast_rows', 'model_tree_reconstructions'):
            summary[key] = 0
        summary['arms']['C_net_margin_veto'].update(fills=0, ending_cash=10, locked_shares=0)
        self.assertTrue(self.state(summary)['strategy_evaluated'])

    def test_real_counts_derive_from_configuration_not_fixed_dataset_constants(self):
        self.config['training_binding']['expected_test_cohort']['scored_rows'] = 9
        self.assertFalse(self.state()['strategy_evaluated'])
        summary = copy.deepcopy(self.summary); summary['model_scored_rows'] = 9
        self.assertTrue(self.state(summary)['strategy_evaluated'])

    def test_missing_boolean_noninteger_over_budget_and_mismatched_counts_fail(self):
        for key in ('actual_model_fit_calls', 'actual_model_predict_calls', 'net_margin_model_fit_calls',
                    'net_margin_model_predict_calls', 'model_tree_reconstructions', 'control_model_fit_calls',
                    'control_model_predict_calls', 'capacity_model_fit_calls', 'capacity_model_predict_calls',
                    'model_scored_rows', 'model_forecast_rows', 'gate_rows', 'producer_replayed_cells',
                    'producer_replay_completions', 'replayed_cells'):
            for value in (None, True, -1, 1.0, 80):
                with self.subTest(key=key, value=value):
                    summary = copy.deepcopy(self.summary); summary[key] = value
                    self.assertFalse(self.state(summary)['strategy_evaluated'])
        summary = copy.deepcopy(self.summary); summary['actual_model_predict_calls'] = 1
        self.assertFalse(self.state(summary)['strategy_evaluated'])

    def test_incomplete_baseline_or_pair_and_wrong_reader_never_qualify(self):
        for field in ('baseline_control_reproduced', 'prefix_control_reproduced', 'control_execution_reproduced'):
            summary = copy.deepcopy(self.summary); summary[field] = False
            self.assertFalse(self.state(summary)['strategy_evaluated'])
        for arm in self.summary['arms']:
            summary = copy.deepcopy(self.summary); del summary['arms'][arm]
            self.assertFalse(self.state(summary)['strategy_evaluated'])
        for independent in ({'passed': True}, {**self.independent, 'independent_ledger_reconstructions': True}, {**self.independent, 'passed': False}):
            self.assertFalse(self.state(independent=independent)['strategy_evaluated'])

    def test_both_restoration_receipts_and_outcomes_required(self):
        for kwargs in ({'inputs': {}}, {'scoped_inputs': {}}, {'scoped_inputs': None}, {'input_outcome': 'failure'},
                       {'evaluation_outcome': 'failure'}, {'independent_outcome': 'failure'}):
            self.assertFalse(self.state(**kwargs)['strategy_evaluated'])

    def test_failure_preserves_zero_nonzero_unknown_actual_counters(self):
        summary = {'actual_model_fit_calls': 2, 'actual_model_predict_calls': 1, 'model_scored_rows': 0, 'replayed_cells': 1}
        state = self.state(summary, evaluation_outcome='failure')
        self.assertEqual(state['actual_model_fit_calls'], 2)
        self.assertEqual(state['actual_model_predict_calls'], 1)
        self.assertEqual(state['model_scored_rows'], 0)
        self.assertIsNone(state['net_margin_model_fit_calls'])
        self.assertIsNone(state['gate_rows'])
        self.assertFalse(state['strategy_evaluated'])


class NetMarginTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.saved_config, self.caps, self.descriptors, self.envelopes, self.raw = fixtures.make_inputs(self.root)
        self.training = {key: {} for key in prepare.TRAINING_KEYS}
        self.training.update(cache_bindings=self.saved_config['cache_bindings'], private_inputs=self.saved_config['private_inputs'])
        self.script = 'print("synthetic auditor")\n'
        self.candidate = b'synthetic\n'
        self.scientific = {'evaluation_mode': 'offline_net_margin', 'live_orders_enabled': False,
            'source_repository': 'yt-feng/poly', 'source_commit': 'f' * 40, 'expected_code_commit': fixtures.CODE,
            'candidate_binding': {'bytes': len(self.candidate), 'sha256': hashlib.sha256(self.candidate).hexdigest()},
            'training_binding': self.training, 'training_binding_sha256': hashlib.sha256(vault.canonical(self.training)).hexdigest()}
        self.private, self.bootstrap = self.root / 'private', self.root / 'bootstrap'

    def packets(self, mutate_config=None, envelope_format='net-margin-private-audit-v1'):
        scientific = copy.deepcopy(self.scientific)
        if mutate_config: mutate_config(scientific)
        registration = {'scientific_config': scientific}
        config = {**scientific, 'independent_audit': {'helper_sha256': hashlib.sha256(self.script.encode()).hexdigest(),
            'registration_canonical_sha256': hashlib.sha256(vault.canonical(registration)).hexdigest()}}
        audit = {'format': envelope_format, 'script_utf8': self.script, 'registration': registration}
        encode = lambda raw: base64.b64encode(lzma.compress(raw)).decode()
        raw = vault.canonical(config)
        return [encode(raw), encode(self.candidate), encode(vault.canonical(audit)),
                *[base64.b64encode(vault.canonical(self.caps[kind])).decode() for kind in ('dataset', 'control')],
                hashlib.sha256(raw).hexdigest()]

    def materialize(self, packets=None):
        fake = SimpleNamespace(decode_candidate_packet=lambda value, binding: self.candidate)
        with mock.patch.object(common, 'verify_code_bindings'), mock.patch.dict(sys.modules, {'offline_exit_retry_audit': fake}):
            return prepare.materialize(*(packets or self.packets()), fixtures.CODE, self.private, self.bootstrap,
                fixtures.REPOSITORY)

    def test_two_narrow_inputs_restore_exactly_grants_bootstrap_only_and_immutable(self):
        self.materialize()
        view = json.loads((self.bootstrap / 'scoped-restore-config.json').read_bytes())
        self.assertEqual(set(view), {'expected_code_commit', 'cache_bindings', 'private_inputs'})
        calls = []
        def fetch(source, maximum, **kwargs):
            calls.append(source)
            kind = next(k for k in self.envelopes if source == view['private_inputs'][k]['source'])
            return self.envelopes[kind]
        receipt = restore.restore_inputs(view, self.caps, self.bootstrap, repository=fixtures.REPOSITORY,
            code_commit=fixtures.CODE, fetch=fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual((self.bootstrap / 'dataset/features.csv').read_bytes(), self.raw['dataset']['features.csv'])
        self.assertEqual((self.bootstrap / 'control/control-predictions.csv').read_bytes(), self.raw['control']['control-predictions.csv'])
        self.assertFalse((self.private / 'results').exists())
        self.assertEqual(json.loads((self.private / 'input-receipt.json').read_bytes())['format'], 'net-margin-input-receipt-v1')
        for name in ('dataset-capability.json', 'control-capability.json', 'scoped-restore-config.json', 'reader.py', 'registration.json', 'candidates.csv'):
            self.assertEqual((self.bootstrap / name).stat().st_mode & 0o777, 0o600)
            self.assertFalse((self.private / name).exists())
        before = (self.bootstrap / 'reader.py').read_bytes()
        with self.assertRaises(FileExistsError): self.materialize()
        self.assertEqual((self.bootstrap / 'reader.py').read_bytes(), before)

    def test_bad_control_grant_rejects_before_common_materialization(self):
        packets = self.packets(); packets[4] = packets[3]
        with mock.patch.object(common, 'materialize') as materialize:
            with self.assertRaises(ValueError): self.materialize(packets)
        materialize.assert_not_called()
        self.assertFalse(self.private.exists()); self.assertFalse(self.bootstrap.exists())

    def test_scoped_hash_source_extra_fields_and_registration_namespace_fail_early(self):
        mutations = [lambda c: c.update(training_binding_sha256='0' * 64),
            lambda c: c['training_binding'].update(extra={}),
            lambda c: c['training_binding']['private_inputs']['control']['source'].update(repository='outside/repository')]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(ValueError): self.materialize(self.packets(mutate))
                self.assertFalse(self.private.exists())
        with self.assertRaises(ValueError): self.materialize(self.packets(envelope_format='price-calibration-private-audit-v1'))
        self.assertFalse(self.private.exists())

    def test_immutable_source_restrictions_checked_after_binding_hash(self):
        for field, value in (('repository', 'outside/repo'), ('commit', 'main'), ('path', '../part-0000.vault')):
            def mutate(config, field=field, value=value):
                config['training_binding']['private_inputs']['control']['source'][field] = value
                config['training_binding_sha256'] = hashlib.sha256(vault.canonical(config['training_binding'])).hexdigest()
            with self.subTest(field=field), mock.patch.object(common, 'materialize') as downstream:
                with self.assertRaisesRegex(ValueError, 'immutable scoped input source'):
                    self.materialize(self.packets(mutate))
                downstream.assert_not_called()

    def test_configuration_and_capability_decode_budgets(self):
        packets = self.packets(); packets[5] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'digest'): self.materialize(packets)
        for value in ('x' * 45001, base64.b64encode(b' ' * 16385).decode(), 'not-base64'):
            packets = self.packets(); packets[4] = value
            with self.assertRaises(ValueError): self.materialize(packets)
        self.assertFalse(self.private.exists())

    def test_masks_all_source_pins_and_paths_before_environment_write(self):
        result = self.materialize()
        env_file = self.root / 'environment'
        env = dict.fromkeys(('AUDIT_CONFIG_B64', 'CANDIDATE_INPUT_B64', 'PRIVATE_AUDIT_B64',
            'DATA_CAPABILITY_B64', 'CONTROL_CAPABILITY_B64', 'EXPECTED_SHA256', 'GITHUB_SHA', 'GITHUB_REPOSITORY'), 'fixture')
        env['GITHUB_ENV'] = str(env_file)
        args = SimpleNamespace(private=self.private, bootstrap=self.bootstrap)
        output = io.StringIO()
        original_open = open
        def open_after_masks(path, *arguments, **kwargs):
            if str(path) == str(env_file):
                for value in result['mask_values']: self.assertIn('::add-mask::' + value + '\n', output.getvalue())
            return original_open(path, *arguments, **kwargs)
        with mock.patch.object(prepare.argparse.ArgumentParser, 'parse_args', return_value=args), \
             mock.patch.object(prepare, 'materialize', return_value=result), mock.patch.dict(os.environ, env), \
             mock.patch('sys.stdout', output), mock.patch('builtins.open', side_effect=open_after_masks):
            self.assertEqual(prepare.main(), 0)
        self.assertEqual(set(line.split('=')[0] for line in env_file.read_text().splitlines()), {'AUDIT_SOURCE_COMMIT', 'PRIVATE_AUDIT_SHA'})
        self.assertNotIn('content_key_b64', env_file.read_text())

    def test_scoped_receipt_is_copied_by_seal_after_analysis_not_in_audited_packet(self):
        self.materialize()
        receipt = {'format': 'quality-capacity-saved-inputs-v1', 'status': 'complete', 'inputs': {}}
        (self.private / 'scoped-input-receipt.json').write_bytes(vault.canonical(receipt))
        (self.private / 'results').mkdir()
        self.assertFalse((self.private / 'results/setup-scoped-input-receipt.json').exists())
        def inspect(private, output, recipient, recipient_id, **kwargs):
            self.assertEqual(json.loads((private / 'results/setup-scoped-input-receipt.json').read_bytes()), receipt)
            self.assertEqual(kwargs['max_member_bytes'], 256 * 1024 ** 2)
            return {'full_evidence_retained': True}
        with mock.patch.object(sealer.delivery, 'seal_outcomes', side_effect=inspect):
            sealer.seal(self.private, self.root / 'sealed', {}, 'synthetic', {'INPUT_OUTCOME': 'success'})
        self.assertFalse((self.private / 'results/dataset-capability.json').exists())


class NetMarginCiphertextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        template = NetMarginStateTests('runTest'); template.setUp()
        self.private = self.root / 'private'; self.private.mkdir(mode=0o700)
        (self.private / 'results').mkdir(mode=0o700)
        self.original_scores = b'market_id,predicted_net_margin_per_share\nsynthetic,-0.1\n'
        for name, value in [('config.json', template.config), ('input-receipt.json', {'status': 'complete'}),
                            ('scoped-input-receipt.json', {'status': 'complete', 'global_identity_unlocks': 0}),
                            ('diagnostics.txt', template.independent), ('results/summary.json', template.summary)]:
            (self.private / name).write_bytes(vault.canonical(value))
        (self.private / 'results/scores.csv').write_bytes(self.original_scores)
        self.key = vault.X25519PrivateKey.generate(); self.recipient = vault.recipient_for(self.key)
        self.target = self.root / 'sealed'
        self.env = {'EVALUATION_OUTCOME': 'success', 'INDEPENDENT_OUTCOME': 'success', 'INPUT_OUTCOME': 'success'}

    def recover_synthetic_cipher(self):
        checked = journal.inspect_delivery(self.target, self.recipient['recipient_id'])
        parts = [json.loads(vault.unseal(path.read_bytes(), self.key)) for path in sorted(self.target.glob('part-*.vault'))]
        raw = b''.join(base64.b64decode(part['payload_base64']) for part in parts)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
        return checked, files

    def test_negative_comparison_really_seals_complete_and_publisher_accepts(self):
        result = sealer.seal(self.private, self.target, self.recipient, self.recipient['recipient_id'], self.env)
        self.assertTrue(result['full_evidence_retained'])
        checked, files = self.recover_synthetic_cipher()
        self.assertTrue(checked['status']['strategy_evaluated'])
        run = json.loads(files['run.json'])
        self.assertEqual(run['format'], 'offline-net-margin-sealed-v1')
        self.assertEqual(run['actual_model_fit_calls'], 2)
        self.assertEqual(run['model_scored_rows'], 8)
        self.assertTrue(run['full_evidence_retained'])
        self.assertEqual(json.loads(files['results/setup-scoped-input-receipt.json'])['global_identity_unlocks'], 0)
        self.assertFalse(any('capability' in name or 'candidates.csv' in name or 'reader.py' in name for name in files))

    def test_size_failure_really_seals_compact_unqualified_and_keeps_summary(self):
        original = sealer.delivery.bundle.make_bundle; calls = []
        def pack(path, **kwargs):
            calls.append((path, kwargs))
            if len(calls) == 1: raise ValueError('RESULT_SIZE_LIMIT')
            return original(path, **kwargs)
        with mock.patch.object(sealer.delivery.bundle, 'make_bundle', side_effect=pack):
            result = sealer.seal(self.private, self.target, self.recipient, self.recipient['recipient_id'], self.env)
        self.assertTrue(result['delivery_complete'])
        self.assertFalse(result['full_evidence_retained'])
        self.assertEqual(calls[0][1]['max_member_bytes'], 256 * 1024 ** 2)
        checked, files = self.recover_synthetic_cipher()
        self.assertFalse(checked['status']['strategy_evaluated'])
        run = json.loads(files['run.json'])
        self.assertFalse(run['full_evidence_retained'])
        self.assertFalse(run['comparison_valid'])
        self.assertEqual(run['analysis_step_outcome'], 'success')
        self.assertEqual(json.loads(files['results/summary.json'])['actual_model_fit_calls'], 2)
        self.assertEqual(self.private.joinpath('results/scores.csv').read_bytes(), self.original_scores)


class NetMarginWorkflowTests(unittest.TestCase):
    def test_workflow_scopes_and_phase_order_and_failures_retained_before_gate(self):
        text = (ROOT / '.github/workflows/offline-net-margin-audit.yml').read_text()
        for value in ('NET_MARGIN_CONFIG_B64', 'NET_MARGIN_CANDIDATES_B64', 'NET_MARGIN_PRIVATE_AUDIT_B64',
                      'SCOPED_RESEARCH_DATA_CAPABILITY_B64', 'SCOPED_RESEARCH_CONTROL_CAPABILITY_B64'):
            self.assertIn('secrets.' + value, text)
        self.assertLess(text.index('id: inputs'), text.index('Remove scoped grants'))
        self.assertLess(text.index('Remove scoped grants'), text.index('id: evaluate'))
        self.assertLess(text.index('id: evaluate'), text.index('id: independent'))
        self.assertLess(text.index('id: independent'), text.index('Remove input bootstrap'))
        self.assertLess(text.index('Remove input bootstrap'), text.index('id: seal'))
        self.assertLess(text.index('id: seal'), text.index('id: retain'))
        self.assertLess(text.index('id: retain'), text.index('id: persist'))
        self.assertLess(text.index('id: persist'), text.index('Require full independently checked evidence'))
        self.assertIn('INPUT_OUTCOME: ${{ steps.inputs.outcome }}', text)
        self.assertIn('EVALUATION_MODE: offline_net_margin', text)
        self.assertIn('COMPARISON_EVALUATED', text)
        self.assertNotIn('cancel-in-progress: true', text)
        self.assertIn('name: Remove runner-local plaintext and temporary archive\n        if: always()', text)
        self.assertNotIn('path: ${{ runner.temp }}', text)
        self.assertEqual(text.count('--dataset-root "$RUNNER_TEMP/net-margin-bootstrap/dataset"'), 2)
        self.assertEqual(text.count('--control-root "$RUNNER_TEMP/net-margin-bootstrap/control"'), 2)
        command = next(line for line in text.splitlines() if 'run: python -m pytest' in line)
        for test in ('test_net_margin_delivery.py', 'test_offline_net_margin_audit.py', 'test_cached_quote_decision_veto.py'):
            self.assertEqual(command.count(test), 1)


if __name__ == '__main__':
    unittest.main()
