"""Keep scheduled forward audit artifacts generic; raw evidence is sealed elsewhere."""
from pathlib import Path
import re
import unittest


class CanaryWorkflowPrivacyTests(unittest.TestCase):
    def test_artifact_allowlist_excludes_raw_forward_inputs(self):
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" /
                    "canary-forward-audit.yml").read_text()
        artifact_path = workflow.rsplit("path: |", 1)[1].split("retention-days", 1)[0]
        self.assertIn("forward_inputs/deployment_status.json", artifact_path)
        self.assertIsNone(re.search(r"^\s+forward_inputs/\s*$", artifact_path, re.MULTILINE))
        self.assertIn("> forward_inputs/event_evidence_stdout.txt 2>&1", workflow)
        self.assertIn("> forward_inputs/outcome_evidence_stdout.txt 2>&1", workflow)
        for forbidden in ("event_evidence", "outcome_evidence", "raw-", "proxy_wallet",
                          "transaction_hash"):
            self.assertNotIn(forbidden, artifact_path)


if __name__ == "__main__":
    unittest.main()
