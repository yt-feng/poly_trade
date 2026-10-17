import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from causal_path_book_pressure_alignment import evaluate, variant_frame


class CausalPathBookPressureAlignmentTests(unittest.TestCase):
    def test_same_side_pressure_gate_keeps_only_nonnegative_pressure(self):
        features = pd.DataFrame(
            {
                "path_efficiency_first2m": [0.6, 0.6, 0.4, 0.6, 0.6],
                "v1_adaptive_mix_side": ["buy_up", "buy_down", "buy_up", "skip", "buy_down"],
                "v1_adaptive_mix_minute": [2, 2, 2, -1, 2],
                "v1_adaptive_mix_fraction": [0.04, 0.04, 0.04, 0.0, 0.04],
                "book_pressure_up_2m": [0.1, -0.2, 0.9, 0.2, 0.2],
                "book_pressure_down_2m": [-0.1, 0.2, 0.2, 0.2, float("nan")],
            }
        )
        result = variant_frame(features, "book_pressure_aligned")
        self.assertEqual(result["v1_adaptive_mix_side"].tolist(), ["buy_up", "buy_down", "skip", "skip", "skip"])
        self.assertEqual(result["v1_adaptive_mix_minute"].tolist(), [2, 2, -1, -1, -1])

    def test_wrong_source_commit_is_rejected_before_data_loading(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            source = repo / "data" / "monthly_runs"
            source.mkdir(parents=True)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
            (repo / "marker").write_text("fixture")
            subprocess.run(["git", "-C", str(repo), "add", "marker"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
            with self.assertRaisesRegex(RuntimeError, "SOURCE_SNAPSHOT_COMMIT_MISMATCH"):
                evaluate(source, repo / "out", "0" * 40)


if __name__ == "__main__":
    unittest.main()
