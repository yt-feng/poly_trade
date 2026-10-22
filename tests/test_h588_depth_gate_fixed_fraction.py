from types import SimpleNamespace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from h588_depth_gate_fixed_fraction import DEPTH_RATIO_FLOOR, FIXED_CASH_FRACTION, depth_capacity_ratio


def _row(depth: float):
    return SimpleNamespace(side="up", fill_price=0.5, sell_up_size_2m=depth, sell_down_size_2m=depth)


def test_h588_binds_the_preregistered_gate_and_fraction():
    row = _row(200.0)
    ratio = depth_capacity_ratio(row, 10.0)
    assert ratio > 10.0
    assert ratio >= DEPTH_RATIO_FLOOR
    assert FIXED_CASH_FRACTION == 0.75


def test_h588_rejects_low_capacity_rows():
    assert depth_capacity_ratio(_row(49.0), 10.0) < DEPTH_RATIO_FLOOR
