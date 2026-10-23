"""Synthetic acceptance and transport boundaries for a paired price correction."""
import base64
import copy
import hashlib
import json
import lzma
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import prepare_exit_retry_inputs as prepare
from research_vault import canonical
from seal_exit_retry_outcomes import evaluation_state


class PriceCalibrationDeliveryContracts(unittest.TestCase):
    def setUp(self):
        self.config = {'evaluation_mode': 'offline_price_calibration',
            'parent_execution_config': {'parent_strategy_config': {'expected_control_candidate_count': 12}}}
        ledger = {'scope': 'pooled_single_ledger', 'block_index': 0}
        self.summary = {'strategy_evaluated': True, 'comparison_valid': True,
            'baseline_control_reproduced': True, 'prefix_control_reproduced': True,
            'evaluation_status': 'evaluated', 'candidate_rows': 12,
            'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0,
            'replayed_cells': 2, 'producer_replay_completions': 2,
            'calibration_forecast_transforms': 12, 'reference_label_updates': 9,
            'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False,
            'arms': {'C_scheduled_retry_fullstream': {**ledger, 'ending_cash': 3},
                     'C_price_residual_calibration': {**ledger, 'fills': 4,
                        'ending_cash': 2, 'locked_shares': 5, 'full_sequence_net_growth': False}}}

    def state(self, summary=None, **overrides):
        return evaluation_state(self.config, self.summary if summary is None else summary,
            overrides.get('independent', {'passed': True}), overrides.get('inputs', {'status': 'complete'}),
            overrides.get('evaluation_outcome', 'success'), overrides.get('independent_outcome', 'success'),
            overrides.get('input_outcome', 'success'))

    def test_negative_or_short_treatment_remains_complete_evidence(self):
        state = self.state()
        self.assertTrue(state['strategy_evaluated'])
        self.assertEqual(state['calibration_forecast_transforms'], 12)
        self.assertEqual(state['reference_label_updates'], 9)

    def test_complete_baseline_and_both_ledgers_required(self):
        for field in ('baseline_control_reproduced', 'prefix_control_reproduced'):
            value = copy.deepcopy(self.summary); value[field] = False
            self.assertFalse(self.state(value)['strategy_evaluated'])
        for arm in self.summary['arms']:
            value = copy.deepcopy(self.summary); del value['arms'][arm]
            self.assertFalse(self.state(value)['strategy_evaluated'])
            value = copy.deepcopy(self.summary); value['arms'][arm]['scope'] = 'reset_account'
            self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_transform_and_replay_counts_cannot_be_hidden_or_boolean(self):
        expected = {'replayed_cells': 2, 'producer_replay_completions': 2,
                    'calibration_forecast_transforms': 12}
        for field, correct in expected.items():
            for invalid in (None, True, 0, correct - 1, float(correct), correct + 1):
                value = copy.deepcopy(self.summary); value[field] = invalid
                self.assertFalse(self.state(value)['strategy_evaluated'], (field, invalid))
        for invalid in (None, True, -1, 13, 1.0):
            value = copy.deepcopy(self.summary); value['reference_label_updates'] = invalid
            self.assertFalse(self.state(value)['strategy_evaluated'])

    def test_missing_audit_or_receipt_cannot_qualify(self):
        self.assertFalse(self.state(independent={'passed': False})['strategy_evaluated'])
        self.assertFalse(self.state(inputs={})['strategy_evaluated'])
        self.assertFalse(self.state(evaluation_outcome='failure')['strategy_evaluated'])

    def test_bad_parent_still_seals_as_failure(self):
        for value in (None, [], 7, {}, {'parent_strategy_config': None}):
            self.config['parent_execution_config'] = value
            self.assertFalse(self.state()['strategy_evaluated'])

    def packet(self, envelope_format='price-calibration-private-audit-v1'):
        script = 'print("synthetic reader")\n'; candidate = b'synthetic\n'
        scientific = {'evaluation_mode': 'offline_price_calibration', 'live_orders_enabled': False,
            'source_repository': 'yt-feng/poly', 'source_commit': 'a' * 40,
            'candidate_binding': {'bytes': len(candidate), 'sha256': hashlib.sha256(candidate).hexdigest()}}
        registration = {'scientific_config': scientific}
        config = {**scientific, 'independent_audit': {'helper_sha256': hashlib.sha256(script.encode()).hexdigest(),
            'registration_canonical_sha256': hashlib.sha256(canonical(registration)).hexdigest()}}
        envelope = {'format': envelope_format, 'script_utf8': script, 'registration': registration}
        encode = lambda raw: base64.b64encode(lzma.compress(raw)).decode()
        raw = canonical(config)
        return encode(raw), encode(candidate), encode(canonical(envelope)), hashlib.sha256(raw).hexdigest()

    def test_new_envelope_and_receipt_are_isolated_from_previous_modes(self):
        fake = SimpleNamespace(decode_candidate_packet=lambda value, binding: b'synthetic\n')
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(prepare, 'verify_code_bindings'), \
                mock.patch.dict(sys.modules, {'offline_exit_retry_audit': fake}):
            private, bootstrap = Path(folder) / 'private', Path(folder) / 'bootstrap'
            prepare.materialize(*self.packet(), 'b' * 40, private, bootstrap,
                                evaluation_mode='offline_price_calibration')
            receipt = json.loads((private / 'input-receipt.json').read_text())
            self.assertEqual(receipt['format'], 'price-calibration-input-receipt-v1')
            for index, old in enumerate(('exit-continuation-private-audit-v1', 'exit-retry-private-audit-v1')):
                destination = Path(folder) / str(index)
                with self.assertRaises(ValueError):
                    prepare.materialize(*self.packet(old), 'b' * 40, destination, Path(folder) / f'b{index}',
                                        evaluation_mode='offline_price_calibration')
                self.assertFalse(destination.exists())

    def test_workflow_uses_distinct_inputs_and_preserves_evidence_before_gate(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/offline-price-calibration-audit.yml').read_text()
        self.assertIn('--evaluation-mode offline_price_calibration', text)
        self.assertIn('PRICE_CALIBRATION_PRIVATE_AUDIT_B64', text)
        self.assertNotIn('secrets.EXIT_CONTINUATION_', text)
        self.assertLess(text.index('id: retain'), text.index('id: persist'))
        self.assertLess(text.index('id: persist'), text.index('Require full independently checked evidence'))
        self.assertIn("'offline-policy-audit'", text)
        self.assertIn('name: Remove runner-local plaintext and temporary archive\n        if: always()', text)


if __name__ == '__main__':
    unittest.main()
