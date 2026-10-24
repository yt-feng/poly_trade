import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import run_corpus_extract as extract


class CorpusExtractTimeoutContractTests(unittest.TestCase):
    def test_parent_timeout_has_grace_over_worker_budget(self):
        self.assertEqual(extract.WORKER_MAX_SECONDS, 480)
        self.assertEqual(extract.WORKER_PROCESS_TIMEOUT_SECONDS, 495)
        self.assertGreater(extract.WORKER_PROCESS_TIMEOUT_SECONDS, extract.WORKER_MAX_SECONDS)


if __name__ == '__main__':
    unittest.main()
