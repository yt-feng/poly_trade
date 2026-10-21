import unittest

from analysis.h440_h434_rolling_stability import _cell_summary, rolling_windows


class H440RollingStabilityTests(unittest.TestCase):
    def test_windows_are_predeclared_and_nonconcatenated(self):
        rows = rolling_windows([f"run-{i}" for i in range(17)], window_runs=12, stride=4)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["runs"][0], "run-0")
        self.assertEqual(rows[0]["runs"][-1], "run-11")
        self.assertEqual(rows[1]["runs"][0], "run-4")
        self.assertEqual(rows[1]["runs"][-1], "run-15")

    def test_cell_gate_uses_passed_window_fraction(self):
        rows = [
            {"slippage": 0.005, "first_ten_passed": True, "first_ten_ending_cash": 12, "first_ten_fills": 10},
            {"slippage": 0.005, "first_ten_passed": True, "first_ten_ending_cash": 13, "first_ten_fills": 10},
            {"slippage": 0.005, "first_ten_passed": False, "first_ten_ending_cash": 9, "first_ten_fills": 3},
            {"slippage": 0.005, "first_ten_passed": True, "first_ten_ending_cash": 11, "first_ten_fills": 10},
            {"slippage": 0.005, "first_ten_passed": True, "first_ten_ending_cash": 14, "first_ten_fills": 10},
        ]
        summary = _cell_summary(rows, 0.005)
        self.assertEqual(summary["window_count"], 5)
        self.assertEqual(summary["growth_positive_windows"], 4)
        self.assertTrue(summary["robust_window_gate"])
        self.assertEqual(summary["minimum_first_ten_fills"], 3)


if __name__ == "__main__":
    unittest.main()
