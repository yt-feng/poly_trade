from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h567_depth_profit_synergy import combined_fraction  # noqa: E402


def _row(depth: float):
    return SimpleNamespace(
        side="up",
        fill_price=0.5,
        sell_up_size_2m=depth,
        sell_down_size_2m=depth,
    )


def test_low_depth_uses_base_then_released_profit_boost():
    chosen0, depth0, profit0, _ = combined_fraction(_row(1.0), 10.0, 0)
    chosen1, depth1, profit1, _ = combined_fraction(_row(1.0), 10.0, 1)
    assert depth0 == depth1 == 0.5
    assert profit0 == 0.55
    assert profit1 == 0.5875
    assert chosen0 == 0.55
    assert chosen1 == 0.5875


def test_high_entry_depth_keeps_depth_fraction_above_profit_boost():
    chosen, depth_fraction, profit_fraction, ratio = combined_fraction(_row(200.0), 10.0, 1)
    assert ratio >= 8.0
    assert depth_fraction == 1.0
    assert profit_fraction == 0.5875
    assert chosen == 1.0
