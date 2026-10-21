import unittest

import pandas as pd

from analysis.h434_old_clean_extratrees_high_confidence import apply_confidence_gate


class H434HighConfidenceTests(unittest.TestCase):
    def test_fixed_gate_keeps_only_high_confidence_rows(self):
        candidates = pd.DataFrame({"q_side": [0.79, 0.80, 0.95], "slug": ["a", "b", "c"]})
        result = apply_confidence_gate(candidates)
        self.assertEqual(result["slug"].tolist(), ["b", "c"])

    def test_gate_is_not_a_directional_side_effect(self):
        candidates = pd.DataFrame({"q_side": [0.80], "side": ["down"]})
        result = apply_confidence_gate(candidates)
        self.assertEqual(result.iloc[0]["side"], "down")

    def test_invalid_gate_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_confidence_gate(pd.DataFrame({"q_side": [0.8]}), 1.0)


if __name__ == "__main__":
    unittest.main()
