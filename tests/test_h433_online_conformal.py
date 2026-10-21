import unittest

import numpy as np

from analysis.h433_old_clean_conformal_extratrees import conformal_threshold


class H433ConformalTests(unittest.TestCase):
    def test_requires_a_causal_calibration_history(self):
        self.assertIsNone(conformal_threshold(np.full(199, 0.2)))

    def test_threshold_is_conservative_and_finite(self):
        scores = np.linspace(0.05, 0.45, 200)
        threshold = conformal_threshold(scores, alpha=0.10)
        self.assertIsNotNone(threshold)
        self.assertGreaterEqual(threshold, 0.50)
        self.assertLessEqual(threshold, 1.0)

    def test_bad_recent_calibration_does_not_lower_confidence_floor(self):
        threshold = conformal_threshold(np.full(200, 0.9))
        self.assertEqual(threshold, 0.50)


if __name__ == "__main__":
    unittest.main()
