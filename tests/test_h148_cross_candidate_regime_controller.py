import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h148_cross_candidate_regime_controller import (
    choose_from_previous,
    current_window_passes,
)
from h56_rolling_window_stability import CONFIGS


def rows(candidate, cash=12.0, passed=True):
    return [
        {
            "candidate": candidate,
            "first_ten_passed": passed,
            "first_ten_fills": 10,
            "first_ten_ending_cash_usd": cash + index / 100,
        }
        for index, _config in enumerate(CONFIGS)
    ]


class H148ControllerTests(unittest.TestCase):
    def test_previous_window_selects_worst_case_cash_leader(self):
        result = choose_from_previous(rows("h30_move_uncertainty", cash=14) + rows("h38_move_uncertainty_midpath", cash=15))
        self.assertEqual(result["candidate"], "h38_move_uncertainty_midpath")
        self.assertEqual(result["gate_id"], "confidence025_edge020")

    def test_failed_candidate_is_not_selected(self):
        result = choose_from_previous(rows("h30_move_uncertainty", passed=False) + rows("h38_move_uncertainty_midpath", cash=11))
        self.assertEqual(result["candidate"], "h38_move_uncertainty_midpath")

    def test_no_passing_previous_candidate_abstains(self):
        self.assertIsNone(choose_from_previous(rows("h30_move_uncertainty", passed=False) + rows("h38_move_uncertainty_midpath", passed=False)))

    def test_current_window_requires_every_declared_configuration(self):
        self.assertTrue(current_window_passes(rows("h30_move_uncertainty")))
        self.assertFalse(current_window_passes(rows("h30_move_uncertainty")[:-1]))
        self.assertFalse(current_window_passes(rows("h30_move_uncertainty", passed=False)))


if __name__ == "__main__":
    unittest.main()
