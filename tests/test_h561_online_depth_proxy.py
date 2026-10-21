from types import SimpleNamespace

import pytest

from analysis.h561_online_depth_proxy import depth_proxy_fraction


def _row(*, side: str, depth: float):
    return SimpleNamespace(
        side=side,
        fill_price=0.5,
        sell_up_size_2m=depth if side == "up" else 999.0,
        sell_down_size_2m=depth if side == "down" else 999.0,
    )


def test_depth_proxy_maps_visible_capacity_monotonically():
    thresholds = (4.0, 6.0, 8.0)
    assert depth_proxy_fraction(_row(side="up", depth=70), 10.0, thresholds)[0] == 0.50
    assert depth_proxy_fraction(_row(side="up", depth=100), 10.0, thresholds)[0] == 0.75
    assert depth_proxy_fraction(_row(side="up", depth=120), 10.0, thresholds)[0] == 0.90
    assert depth_proxy_fraction(_row(side="up", depth=160), 10.0, thresholds)[0] == 1.00


def test_depth_proxy_uses_the_selected_side_only():
    fraction, ratio = depth_proxy_fraction(_row(side="down", depth=70), 10.0, (4.0, 6.0, 8.0))
    assert fraction == 0.50
    assert ratio < 4.0


def test_depth_proxy_rejects_non_monotone_thresholds():
    with pytest.raises(ValueError, match="strictly increasing"):
        depth_proxy_fraction(_row(side="up", depth=100), 10.0, (6.0, 4.0, 8.0))
