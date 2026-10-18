import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h286_redemption_latency_replay import CANDIDATE, DELAYS_SECONDS, SOURCE_COMMIT


def test_h286_freezes_signal_source_and_uses_multi_hour_delays():
    assert CANDIDATE == "h30_move_uncertainty"
    assert SOURCE_COMMIT == "bbda5f2050685f63153621a54641ebdfab943487"
    assert DELAYS_SECONDS == (600.0, 21_600.0, 28_800.0)
    assert all(delay > 600 for delay in DELAYS_SECONDS[1:])
