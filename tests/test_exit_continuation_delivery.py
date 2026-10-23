import base64
import copy
import hashlib
import lzma
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import prepare_exit_retry_inputs as prepare
from seal_exit_retry_outcomes import evaluation_state
from research_vault import canonical


class ContinuationDeliveryContracts(unittest.TestCase):
    def setUp(self):
        self.config = {'evaluation_mode': 'offline_exit_continuation',
                       'parent_strategy_config': {'expected_control_candidate_count': 12}}
        ledger = {'scope': 'pooled_single_ledger', 'block_index': 0}
        self.summary = {'strategy_evaluated': True, 'comparison_valid': True,
            'prefix_control_reproduced': True, 'evaluation_status': 'evaluated',
            'candidate_rows': 12, 'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0,
            'replayed_cells': 1, 'producer_replay_completions': 1,
            'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False,
            'prefix_ledger': {**ledger, 'fills': 10, 'strict_net_growth_ten': True},
            'full_ledger': {**ledger, 'fills': 11, 'full_sequence_net_growth': False, 'locked_shares': 2}}

    def state(self, summary=None, **kwargs):
        return evaluation_state(self.config, self.summary if summary is None else summary,
            kwargs.get('independent', {'passed': True}), kwargs.get('inputs', {'status': 'complete'}),
            kwargs.get('evaluation_outcome', 'success'), kwargs.get('independent_outcome', 'success'),
            kwargs.get('input_outcome', 'success'))

    def test_later_loss_or_lock_remains_valid_complete_negative_evidence(self):
        self.assertTrue(self.state()['strategy_evaluated'])
        value = copy.deepcopy(self.summary)
        value['full_ledger'].update(locked_shares=0, ending_cash=3)
        self.assertTrue(self.state(value)['strategy_evaluated'])

    def test_passing_prefix_does_not_substitute_for_a_full_ledger(self):
        for key in ('prefix_ledger', 'full_ledger'):
            value = copy.deepcopy(self.summary); del value[key]
            self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_new_mode_requires_exactly_one_completed_producer_replay(self):
        for field in ('replayed_cells', 'producer_replay_completions'):
            for invalid in (0, 2, True, 1.0, None):
                value = copy.deepcopy(self.summary); value[field] = invalid
                self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_reset_or_unverified_prefix_cannot_pass(self):
        for key in ('prefix_ledger', 'full_ledger'):
            value = copy.deepcopy(self.summary); value[key]['scope'] = 'reset_block'
            self.assertFalse(self.state(value)['strategy_evaluated'])
        value = copy.deepcopy(self.summary); value['prefix_control_reproduced'] = False
        self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_missing_receipts_or_independent_failure_cannot_pass(self):
        self.assertFalse(self.state(independent={'passed': False})['strategy_evaluated'])
        self.assertFalse(self.state(inputs={})['strategy_evaluated'])
        self.assertFalse(self.state(independent_outcome='failure')['strategy_evaluated'])

    def test_malformed_parent_does_not_prevent_failure_evidence_sealing(self):
        for invalid in (None, [], 7):
            self.config['parent_strategy_config'] = invalid
            self.assertFalse(self.state()['strategy_evaluated'])

    def packet(self, *, mode='offline_exit_continuation', envelope_format='exit-continuation-private-audit-v1'):
        candidate = b'synthetic\n'; script = 'print("synthetic audit")\n'
        scientific = {'evaluation_mode': mode, 'live_orders_enabled': False,
            'source_repository': 'yt-feng/poly', 'source_commit': 'a' * 40,
            'candidate_binding': {'bytes': len(candidate), 'sha256': hashlib.sha256(candidate).hexdigest()}}
        registration = {'scientific_config': scientific}
        config = {**scientific, 'independent_audit': {
            'helper_sha256': hashlib.sha256(script.encode()).hexdigest(),
            'registration_canonical_sha256': hashlib.sha256(canonical(registration)).hexdigest()}}
        envelope = {'format': envelope_format, 'script_utf8': script, 'registration': registration}
        encode = lambda raw: base64.b64encode(lzma.compress(raw)).decode()
        raw = canonical(config)
        return (encode(raw), encode(candidate), encode(canonical(envelope)), hashlib.sha256(raw).hexdigest())

    def restore(self, packet, private, bootstrap):
        fake = SimpleNamespace(decode_candidate_packet=lambda value, binding: b'synthetic\n')
        with mock.patch.object(prepare, 'verify_code_bindings'), \
                mock.patch.dict(sys.modules, {'offline_exit_retry_audit': fake}):
            return prepare.materialize(*packet, 'b' * 40, private, bootstrap,
                                       evaluation_mode='offline_exit_continuation')

    def test_private_restoration_separates_continuation_mode_and_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            private, bootstrap = Path(folder) / 'private', Path(folder) / 'bootstrap'
            self.restore(self.packet(), private, bootstrap)
            self.assertIn('exit-continuation-input-receipt-v1', (private / 'input-receipt.json').read_text())
            self.assertTrue((bootstrap / 'reader.py').is_file())

    def test_retry_mode_or_envelope_cannot_cross_into_continuation(self):
        packets = [self.packet(mode='offline_exit_retry'),
                   self.packet(envelope_format='exit-retry-private-audit-v1')]
        with tempfile.TemporaryDirectory() as folder:
            for i, packet in enumerate(packets):
                private, bootstrap = Path(folder) / f'private{i}', Path(folder) / f'bootstrap{i}'
                with self.assertRaises(ValueError): self.restore(packet, private, bootstrap)
                self.assertFalse(private.exists())

    def test_workflow_retains_and_persists_before_validity_gate(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/offline-exit-continuation-audit.yml').read_text()
        self.assertIn('--evaluation-mode offline_exit_continuation', text)
        self.assertIn('EVALUATION_MODE: offline_exit_continuation', text)
        self.assertIn('EXIT_CONTINUATION_PRIVATE_AUDIT_B64', text)
        self.assertNotIn('secrets.EXIT_RETRY_', text)
        self.assertLess(text.index('id: retain'), text.index('id: persist'))
        self.assertLess(text.index('id: persist'), text.index('Require full independently checked evidence'))
        self.assertIn("'offline-policy-audit'", text)
        self.assertIn('name: Remove runner-local plaintext and temporary archive\n        if: always()', text)


if __name__ == '__main__': unittest.main()
