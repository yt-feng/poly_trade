from decimal import Decimal

from analysis.h402_inventory_balanced_maker import build_strategy


class _Config:
    class StrategyConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def test_h402_freezes_inventory_balanced_paired_protocol():
    strategy = build_strategy(_Config)
    assert strategy.split_size == Decimal("5")
    assert strategy.max_lean_shares == Decimal("1")
    assert strategy.improve_spread_ticks == 1
    assert strategy.fair_edge == Decimal("0")
    assert strategy.maker_rebate_bps == Decimal("0")
