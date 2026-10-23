"""Evidence enrichment must not claim strategy evaluation or publish plaintext."""
from pathlib import Path
import re
import tempfile
import unittest

import test_offline_paired_workflow as shared
import test_offline_histogram_residual_workflow as transport

ROOT = Path(__file__).resolve().parents[1]


class ArchivedQuoteCashDomainWorkflowTests(unittest.TestCase):
    private_directory = 'archived-quote-cash-domain-export'
    _run_sealing = shared.PairedWorkflowTests._run_sealing
    run_materialization = transport.HistogramResidualWorkflowTests.run_materialization
    test_compressed_private_input_roundtrip_exact_digest_and_permissions = (
        transport.HistogramResidualWorkflowTests.test_compressed_private_input_roundtrip_exact_digest_and_permissions)
    test_bad_digest_encoding_stream_and_expansion_never_write_input = (
        transport.HistogramResidualWorkflowTests.test_bad_digest_encoding_stream_and_expansion_never_write_input)

    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/archived-quote-cash-domain-export.yml').read_text()

    def test_main_only_private_input_outputs_and_frozen_helper_contracts(self):
        self.assertIn("if: github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn('group: offline-policy-audit', self.workflow)
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_0-9]+)', self.workflow)),
                         {'ARCHIVED_QUOTE_CASH_DOMAIN_CONFIG_B64'})
        self.assertNotRegex(self.workflow, r'secrets\[|toJSON\(secrets\)|PRIVATE_KEY|WALLET|POLYMARKET_API')
        evaluate = shared.step(self.workflow, 'id: evaluate')
        self.assertIn('analysis/archived_quote_cash_domain_export.py', evaluate)
        self.assertIn('> "$RUNNER_TEMP/archived-quote-cash-domain-export/analysis-output.txt" 2>&1', evaluate)
        self.assertNotRegex(evaluate, r'\btee\b|set\s+-x')
        configuration = shared.step(self.workflow, 'id: configuration')
        for fragment in ('LZMADecompressor', 'memlimit=67108864', 'max_length=262145',
                         'decoder.eof and not decoder.unused_data'):
            self.assertIn(fragment, configuration)
        for package in ('numpy==2.4.6', 'scipy==1.17.1', 'scikit-learn==1.9.1',
                        'pandas==3.1.0', 'joblib==1.6.0', 'threadpoolctl==3.7.0'):
            self.assertIn(package, self.workflow)
        contracts = shared.step(self.workflow, 'id: contracts')
        for name in ('test_timed_quote_execution.py', 'test_offline_bid_residual_audit.py',
                     'test_archived_quote_cash_domain_export.py'):
            self.assertIn(name, contracts)
        ci = (ROOT / '.github/workflows/compound-readiness-contract.yml').read_text()
        self.assertIn('test_archived_quote_cash_domain_export.py', ci)
        self.assertIn('test_archived_quote_cash_domain_workflow.py', ci)

    @staticmethod
    def complete_summary():
        return {'evidence_exported': True, 'export_status': 'complete',
                'all_identity_gates_passed': True, 'strategy_evaluated': False,
                'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0,
                'replayed_cells': 0, 'exported_rows': 3}

    def assert_non_strategy_metadata(self, status, metadata):
        self.assertFalse(status['strategy_evaluated'])
        self.assertFalse(metadata['strategy_evaluated'])
        self.assertFalse(metadata['new_data_collected'])
        self.assertEqual(metadata['allowed_model_fit_calls'], 0)
        self.assertEqual(metadata['allowed_model_predict_calls'], 0)
        self.assertEqual(metadata['allowed_replay_cells'], 0)

    def test_successful_export_is_evidence_only(self):
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, files = self._run_sealing(Path(directory), self.complete_summary())
            self.assert_non_strategy_metadata(status, metadata)
            self.assertTrue(metadata['evidence_exported'])
            self.assertFalse(metadata['model_refit'])
            for field in ('actual_model_fit_calls', 'actual_model_predict_calls', 'replayed_cells'):
                self.assertEqual(metadata[field], 0)
            self.assertEqual(files['frozen_input.json'], b'{"private_protocol": true}')
            self.assertEqual(metadata['evaluation_class'],
                             'registered_old_archive_evidence_enrichment_no_strategy_evaluation')

    def test_failed_incomplete_or_forged_summary_cannot_claim_evidence_success(self):
        variants = [('evidence_exported', False), ('export_status', 'failed'),
                    ('all_identity_gates_passed', False), ('strategy_evaluated', True),
                    ('actual_model_fit_calls', 1), ('actual_model_fit_calls', None),
                    ('actual_model_fit_calls', False), ('actual_model_predict_calls', 1),
                    ('replayed_cells', 1), ('exported_rows', 0), ('exported_rows', True)]
        for field, value in variants:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                status, metadata, _ = self._run_sealing(
                    Path(directory), {**self.complete_summary(), field: value})
                self.assert_non_strategy_metadata(status, metadata)
                self.assertFalse(metadata['evidence_exported'])
                for counter in ('actual_model_fit_calls', 'actual_model_predict_calls', 'replayed_cells'):
                    observed = value if field == counter else 0
                    expected = observed if type(observed) is int and observed >= 0 else None
                    self.assertEqual(metadata[counter], expected)
                self.assertEqual(metadata['model_refit'],
                                 metadata['actual_model_fit_calls'] > 0
                                 if metadata['actual_model_fit_calls'] is not None else None)

    def test_setup_and_export_failures_remain_sealed(self):
        for outcome in ('skipped', 'failure'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                status, metadata, files = self._run_sealing(Path(directory), outcome=outcome)
                self.assert_non_strategy_metadata(status, metadata)
                self.assertFalse(metadata['evidence_exported'])
                self.assertEqual(set(files), {'run.json'})
                for field in ('actual_model_fit_calls', 'actual_model_predict_calls', 'replayed_cells'):
                    self.assertEqual(metadata[field], 0 if outcome == 'skipped' else None)
                self.assertEqual(metadata['model_refit'], False if outcome == 'skipped' else None)
        with tempfile.TemporaryDirectory() as directory:
            status, metadata, _ = self._run_sealing(
                Path(directory), self.complete_summary(), outcome='failure')
            self.assert_non_strategy_metadata(status, metadata)
            self.assertFalse(metadata['evidence_exported'])

    def test_ciphertext_retained_before_publisher_and_plaintext_always_cleaned(self):
        upload = shared.step(self.workflow, 'actions/upload-artifact@')
        publish = shared.step(self.workflow, 'tools/publish_quality_first_journal.py')
        cleanup = shared.step(self.workflow, 'Remove runner-local plaintext')
        self.assertLess(self.workflow.index(upload), self.workflow.index(publish))
        self.assertLess(self.workflow.index(publish), self.workflow.index(cleanup))
        self.assertIn('path: sealed-delivery/', upload)
        self.assertNotRegex(upload, r'config\.json|analysis-output|RUNNER_TEMP|source_poly')
        self.assertIn('if: always()', cleanup)
        self.assertIn("'archived-quote-cash-domain-export'", cleanup)


if __name__ == '__main__':
    unittest.main()
