from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PairArbOldCleanWorkflowContract(unittest.TestCase):
    def test_workflow_is_frozen_and_sealed_without_plaintext_artifact_upload(self):
        workflow = (ROOT / '.github/workflows/pair-arb-old-clean-audit.yml').read_text()
        self.assertIn('bbda5f2050685f63153621a54641ebdfab943487', workflow)
        self.assertIn('--fee-rate 0.07 --slippage 0.0025', workflow)
        self.assertIn('--fee-rate 0 --slippage 0', workflow)
        self.assertIn('--cash-release-delay-seconds 600', workflow)
        self.assertIn('publish_quality_first_journal.py', workflow)
        self.assertNotIn('upload-artifact', workflow)
        self.assertIn("'strategy_evaluated': False", workflow)


if __name__ == '__main__':
    unittest.main()
