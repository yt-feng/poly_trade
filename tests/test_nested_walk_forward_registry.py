import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from nested_walk_forward_registry import TEST_BLOCK_SIZES, outer_folds


class NestedWalkForwardRegistryTests(unittest.TestCase):
    def test_folds_have_expanding_train_and_disjoint_validation_test_windows(self):
        runs = [f"run-{i:02d}" for i in range(83)]
        folds = outer_folds(runs)
        self.assertEqual(len(folds), len(TEST_BLOCK_SIZES))
        tests = [name for fold in folds for name in fold["test_runs"]]
        self.assertEqual(len(tests), sum(TEST_BLOCK_SIZES))
        self.assertEqual(len(tests), len(set(tests)))
        self.assertEqual([len(fold["test_runs"]) for fold in folds], list(TEST_BLOCK_SIZES))
        self.assertEqual([len(fold["validation_runs"]) for fold in folds], [4] * len(TEST_BLOCK_SIZES))
        self.assertEqual([len(fold["train_runs"]) for fold in folds], [14, 27, 40, 53, 66])
        for fold in folds:
            self.assertTrue(set(fold["train_runs"]).isdisjoint(fold["validation_runs"]))
            self.assertTrue(set(fold["train_runs"]).isdisjoint(fold["test_runs"]))
            self.assertTrue(set(fold["validation_runs"]).isdisjoint(fold["test_runs"]))


if __name__ == "__main__":
    unittest.main()
