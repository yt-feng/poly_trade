import base64
import copy
import lzma
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import prepare_exit_retry_inputs as prepare
from prepare_exit_retry_inputs import decode_xz
from seal_exit_retry_outcomes import evaluation_state
import seal_exit_retry_outcomes as sealer
import run_quality_first_research as bundles
from research_vault import canonical


class DeliveryContracts(unittest.TestCase):
    def setUp(self):
        self.config = {'expected_control_candidate_count': 3}
        self.summary = {'strategy_evaluated': True, 'comparison_valid': True,
                        'control_execution_reproduced': True, 'evaluation_status': 'evaluated',
                        'candidate_rows': 3, 'actual_model_fit_calls': 0,
                        'actual_model_predict_calls': 0, 'replayed_cells': 2,
                        'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False,
                        'arms': {name: {'scope': 'pooled_single_ledger', 'block_index': 0,
                                      'economic_pass': False}
                                 for name in ('C_original', 'C_scheduled_retry')}}

    def state(self, summary=None, **outcomes):
        return evaluation_state(self.config, summary if summary is not None else self.summary,
            outcomes.get('independent', {'passed': True}), outcomes.get('inputs', {'status': 'complete'}),
            outcomes.get('evaluation_outcome', 'success'), outcomes.get('independent_outcome', 'success'),
            outcomes.get('input_outcome', 'success'))

    def test_model_free_negative_economics_are_a_valid_evaluation(self):
        self.assertTrue(self.state()['strategy_evaluated'])

    def test_unexpected_fit_or_prediction_invalidates_evaluation(self):
        for field in ('actual_model_fit_calls', 'actual_model_predict_calls'):
            value = copy.deepcopy(self.summary); value[field] = 1
            self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_missing_float_and_boolean_counters_do_not_become_zero(self):
        for invalid in (None, 0.0, False):
            value = copy.deepcopy(self.summary); value['actual_model_fit_calls'] = invalid
            state = self.state(value)
            self.assertIsNone(state['actual_model_fit_calls'])
            self.assertFalse(state['strategy_evaluated'])

    def test_extra_or_reset_ledgers_cannot_satisfy_primary_evidence(self):
        for mutate in ('extra', 'reset', 'bool_index'):
            value = copy.deepcopy(self.summary)
            if mutate == 'extra': value['arms']['extra'] = {'scope': 'pooled_single_ledger', 'block_index': 0}
            if mutate == 'reset': value['arms']['C_scheduled_retry']['scope'] = 'disjoint_diagnostic_block'
            if mutate == 'bool_index': value['arms']['C_scheduled_retry']['block_index'] = False
            self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_audit_inputs_and_control_are_all_required(self):
        self.assertFalse(self.state(independent={'passed': False})['strategy_evaluated'])
        self.assertFalse(self.state(input_outcome='failure')['strategy_evaluated'])
        value = copy.deepcopy(self.summary); value['control_execution_reproduced'] = False
        self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_partial_execution_keeps_observed_replay_count(self):
        self.assertEqual(self.state({'replayed_cells': 1}, evaluation_outcome='failure')['replayed_cells'], 1)
        self.assertIsNone(self.state({}, evaluation_outcome='failure')['replayed_cells'])
        self.assertEqual(self.state({}, evaluation_outcome='skipped')['replayed_cells'], 0)

    def test_bounded_xz_rejects_concatenation_trailing_and_overflow(self):
        raw = b'fixture'; packed = lzma.compress(raw)
        self.assertEqual(decode_xz(base64.b64encode(packed).decode(), len(raw)), raw)
        for invalid in (packed + b'x', packed + packed, packed[:-2]):
            with self.assertRaises(ValueError): decode_xz(base64.b64encode(invalid).decode(), len(raw))
        with self.assertRaises(ValueError): decode_xz(base64.b64encode(packed).decode(), len(raw) - 1)

    def materialization_fixture(self, scientific_tamper=False, reader_tamper=False):
        candidate = b'fixture\n'
        scientific = {'evaluation_mode': 'offline_exit_retry', 'live_orders_enabled': False,
                      'source_repository': 'yt-feng/poly', 'source_commit': 'a' * 40,
                      'candidate_binding': {'bytes': len(candidate), 'sha256': hashlib.sha256(candidate).hexdigest()}}
        registered = copy.deepcopy(scientific)
        if scientific_tamper: registered['live_orders_enabled'] = True
        script = 'print("synthetic reader")\n'
        registration = {'scientific_config': registered}
        config = {**scientific, 'independent_audit': {
            'helper_sha256': hashlib.sha256(script.encode()).hexdigest(),
            'registration_canonical_sha256': hashlib.sha256(canonical(registration)).hexdigest()}}
        if reader_tamper: script += '# modified after binding\n'
        audit = {'format': 'exit-retry-private-audit-v1', 'script_utf8': script, 'registration': registration}
        encoded = lambda raw: base64.b64encode(lzma.compress(raw)).decode()
        raw = canonical(config)
        return (encoded(raw), encoded(candidate), encoded(canonical(audit)), hashlib.sha256(raw).hexdigest())

    def test_restore_rejects_semantic_registration_even_when_envelope_hashes_match(self):
        fixture = self.materialization_fixture(scientific_tamper=True)
        fake_decoder = SimpleNamespace(decode_candidate_packet=lambda value, binding: b'fixture\n')
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(prepare, 'verify_code_bindings'), \
                mock.patch.dict(sys.modules, {'offline_exit_retry_audit': fake_decoder}):
            with self.assertRaisesRegex(ValueError, 'scientific registration mismatch'):
                prepare.materialize(*fixture, 'b' * 40, Path(folder) / 'private', Path(folder) / 'bootstrap')
            self.assertFalse((Path(folder) / 'private').exists())

    def test_restore_requires_exact_reader_bytes_and_never_overwrites_inputs(self):
        fake_decoder = SimpleNamespace(decode_candidate_packet=lambda value, binding: b'fixture\n')
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(prepare, 'verify_code_bindings'), \
                mock.patch.dict(sys.modules, {'offline_exit_retry_audit': fake_decoder}):
            private, bootstrap = Path(folder) / 'private', Path(folder) / 'bootstrap'
            with self.assertRaisesRegex(ValueError, 'independent audit binding mismatch'):
                prepare.materialize(*self.materialization_fixture(reader_tamper=True), 'b' * 40, private, bootstrap)
            prepare.materialize(*self.materialization_fixture(), 'b' * 40, private, bootstrap)
            original = (bootstrap / 'reader.py').read_bytes()
            with self.assertRaises(FileExistsError):
                prepare.materialize(*self.materialization_fixture(), 'b' * 40, private, bootstrap)
            self.assertEqual((bootstrap / 'reader.py').read_bytes(), original)

    def test_workflow_retains_and_persists_before_full_gate_and_always_cleans(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/offline-exit-retry-audit.yml').read_text()
        self.assertLess(text.index('id: retain'), text.index('id: persist'))
        self.assertLess(text.index('id: persist'), text.index('Require full independently checked evidence'))
        self.assertIn("COMPARISON_EVALUATED", text)
        self.assertIn('name: Remove runner-local plaintext and temporary archive\n        if: always()', text)
        self.assertNotIn('cancel-in-progress: true', text)

    def test_setup_failure_and_receipt_reach_the_actual_bundle_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / 'private'; private.mkdir()
            failure = {'stage': 'private_inputs', 'error': 'synthetic binding mismatch'}
            receipt = {'status': 'complete', 'candidate_bytes': 8}
            (private / 'setup-failure.json').write_text(json.dumps(failure))
            (private / 'input-receipt.json').write_text(json.dumps(receipt))

            def inspect_bundle(private, output, recipient, recipient_id, **kwargs):
                # Exercise the real generic bundler, not a mirrored allowlist.
                with tarfile.open(fileobj=io.BytesIO(bundles.make_bundle(private)), mode='r:gz') as archive:
                    self.assertEqual(json.load(archive.extractfile('results/setup-failure.json')), failure)
                    self.assertEqual(json.load(archive.extractfile('results/setup-input-receipt.json')), receipt)
                return {'full_evidence_retained': True}

            with mock.patch.object(sealer.delivery, 'seal_outcomes', side_effect=inspect_bundle):
                sealer.seal(private, Path(folder) / 'sealed', {}, 'fixture', {'INPUT_OUTCOME': 'failure'})

    def test_decoded_private_metadata_is_masked_before_later_steps(self):
        result = {'source_commit': 'a' * 40, 'reader_sha256': 'b' * 64}
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / 'environment'
            environment = dict.fromkeys(('AUDIT_CONFIG_B64', 'CANDIDATE_INPUT_B64',
                'PRIVATE_AUDIT_B64', 'EXPECTED_SHA256', 'GITHUB_SHA'), 'fixture')
            environment['GITHUB_ENV'] = str(env_file)
            args = SimpleNamespace(private=Path(folder) / 'private', bootstrap=Path(folder) / 'bootstrap',
                                   evaluation_mode='offline_exit_retry')
            output = io.StringIO()
            with mock.patch.object(prepare.argparse.ArgumentParser, 'parse_args', return_value=args), \
                    mock.patch.object(prepare, 'materialize', return_value=result), \
                    mock.patch.dict(prepare.os.environ, environment), mock.patch('sys.stdout', output):
                prepare.main()
            for value in (result['source_commit'], result['source_commit'][:7], result['reader_sha256']):
                self.assertIn('::add-mask::' + value + '\n', output.getvalue())
            self.assertIn('AUDIT_SOURCE_COMMIT=' + result['source_commit'], env_file.read_text())


if __name__ == '__main__': unittest.main()
