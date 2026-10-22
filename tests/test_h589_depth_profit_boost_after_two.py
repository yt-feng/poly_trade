from types import SimpleNamespace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from h589_depth_profit_boost_after_two import BOOST_AFTER_PROFITS, THRESHOLDS, combined_fraction


def _row(depth: float):
    return SimpleNamespace(side="up", fill_price=0.5, sell_up_size_2m=depth, sell_down_size_2m=depth)


def test_boost_waits_for_two_released_profits():
    assert THRESHOLDS == (3.0, 5.0, 7.0)
    assert BOOST_AFTER_PROFITS == 2
    one = combined_fraction(_row(1.0), 10.0, 1)
    two = combined_fraction(_row(1.0), 10.0, 2)
    assert one[0] == 0.55
    assert two[0] == 0.5875


def test_high_depth_remains_depth_limited():
    chosen, depth_fraction, profit_fraction, ratio = combined_fraction(_row(200.0), 10.0, 2)
    assert ratio >= 7.0
    assert depth_fraction == 1.0
    assert profit_fraction == 0.5875
    assert chosen == 1.0
