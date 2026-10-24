import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
SPEC = importlib.util.spec_from_file_location(
    "h90_random_forest_small_cash",
    ROOT / "analysis" / "h90_random_forest_small_cash.py",
)
H90 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(H90)


METRICS = {"ending_cash": 5.0, "net_pnl": 0.0, "trades": 0, "blocked": 0, "max_drawdown": 0.0}


class H90ProtocolBoundaryTests(unittest.TestCase):
    def test_selection_scope_is_exactly_twenty_runs(self):
        with self.assertRaisesRegex(ValueError, "selection_block_scope"):
            H90.selection_blocks(None, [str(i) for i in range(19)], 0.005)

    def test_holdout_scope_is_exactly_fifty_nine_runs(self):
        with self.assertRaisesRegex(ValueError, "holdout_block_scope"):
            H90.holdout_blocks(None, [str(i) for i in range(58)], 0.005)

    def test_selection_and_holdout_helpers_keep_scopes_separate(self):
        calls = []

        def fake_replay(_events, run_names, slippage):
            calls.append((tuple(run_names), slippage))
            return dict(METRICS), None

        selection = [f"selection-{i}" for i in range(20)]
        holdout = [f"holdout-{i}" for i in range(59)]
        with patch.object(H90, "replay", side_effect=fake_replay):
            H90.selection_blocks(None, selection, 0.005)
            H90.holdout_blocks(None, holdout, 0.01)

        self.assertEqual(len(calls), 9)
        self.assertEqual(calls[0][0], tuple(selection[:5]))
        self.assertEqual(calls[3][0], tuple(selection[15:20]))
        self.assertEqual(calls[4][0], tuple(holdout[:12]))
        self.assertEqual(calls[-1][0], tuple(holdout[48:59]))
        self.assertTrue(all(slippage == 0.005 for _, slippage in calls[:4]))
        self.assertTrue(all(slippage == 0.01 for _, slippage in calls[4:]))


if __name__ == "__main__":
    unittest.main()
