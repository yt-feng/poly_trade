import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
SPEC = importlib.util.spec_from_file_location(
    "h91_random_forest_stability_first",
    ROOT / "analysis" / "h91_random_forest_stability_first.py",
)
H91 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(H91)


METRICS = {"ending_cash": 5.0, "net_pnl": 0.0, "trades": 0, "blocked": 0, "max_drawdown": 0.0}


class H91ProtocolBoundaryTests(unittest.TestCase):
    def test_selection_scope_is_exactly_twenty_runs(self):
        with self.assertRaisesRegex(ValueError, "selection_block_scope"):
            H91.selection_blocks(None, [str(i) for i in range(19)], 0.005)

    def test_holdout_scope_is_exactly_fifty_nine_runs(self):
        with self.assertRaisesRegex(ValueError, "holdout_block_scope"):
            H91.holdout_blocks(None, [str(i) for i in range(58)], 0.005)

    def test_holdout_blocks_preserve_five_blocks_and_slippage(self):
        calls = []

        def fake_replay(_events, run_names, slippage):
            calls.append((tuple(run_names), slippage))
            return dict(METRICS), None

        holdout = [f"holdout-{i}" for i in range(59)]
        with patch.object(H91, "replay", side_effect=fake_replay):
            result = H91.holdout_blocks(None, holdout, 0.01)

        self.assertEqual(len(result), 5)
        self.assertEqual([row["run_count"] for row in result], [12, 12, 12, 12, 11])
        self.assertEqual(calls[0][0], tuple(holdout[:12]))
        self.assertEqual(calls[-1][0], tuple(holdout[48:59]))
        self.assertTrue(all(slippage == 0.01 for _, slippage in calls))

    def test_selection_blocks_use_frozen_four_by_five_scope(self):
        calls = []

        def fake_replay(_events, run_names, slippage):
            calls.append((tuple(run_names), slippage))
            return dict(METRICS), None

        selection = [f"selection-{i}" for i in range(20)]
        with patch.object(H91, "replay", side_effect=fake_replay):
            result = H91.selection_blocks(None, selection, 0.005)

        self.assertEqual(len(result), 4)
        self.assertEqual([row["run_count"] for row in result], [5, 5, 5, 5])
        self.assertEqual(calls[0][0], tuple(selection[:5]))
        self.assertEqual(calls[-1][0], tuple(selection[15:20]))
        self.assertTrue(all(slippage == 0.005 for _, slippage in calls))


if __name__ == "__main__":
    unittest.main()
