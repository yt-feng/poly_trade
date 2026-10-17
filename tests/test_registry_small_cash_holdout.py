import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from registry_small_cash_holdout import BLOCK_SIZES, REGISTRY_NAMES, holdout_blocks


class RegistrySmallCashHoldoutTests(unittest.TestCase):
    def test_holdout_partition_is_chronological_and_exact(self):
        runs = [f"run-{i:02d}" for i in range(sum(BLOCK_SIZES))]
        blocks = holdout_blocks(runs)
        self.assertEqual([len(block) for block in blocks], list(BLOCK_SIZES))
        self.assertEqual([value for block in blocks for value in block], runs)

    def test_registry_is_fixed_and_unique(self):
        self.assertGreaterEqual(len(REGISTRY_NAMES), 20)
        self.assertEqual(len(REGISTRY_NAMES), len(set(REGISTRY_NAMES)))


if __name__ == "__main__":
    unittest.main()
