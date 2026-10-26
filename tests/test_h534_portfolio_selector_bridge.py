from __future__ import annotations

import math

from analysis.h534_portfolio_selector_bridge import (
    BASE_DEPTH_HAIRCUT,
    BASE_LATENCY_SECONDS,
    BASE_SLIPPAGE,
    FIXED_FRACTION,
    HYPOTHESIS_ID,
    INITIAL_CASH,
    MINIMUM_ORDER,
    REQUIRED_FILLS,
    _h534_cell,
)


def test_bridge_contract_is_frozen():
    assert HYPOTHESIS_ID == "H534_PORTFOLIO_SELECTOR_BRIDGE"
    assert FIXED_FRACTION == 0.55
    assert BASE_SLIPPAGE == 0.005
    assert BASE_LATENCY_SECONDS == 4.0
    assert BASE_DEPTH_HAIRCUT == 0.15
    assert INITIAL_CASH == 10.0
    assert MINIMUM_ORDER == 5.0
    assert REQUIRED_FILLS == 10


def test_h534_cell_requires_selection_and_holdout_rows():
    def row(index: int, cash: float = 11.0):
        return {
            "gross_cash_fraction": 0.55,
            "slippage": 0.005,
            "latency_seconds": 4.0,
            "depth_haircut": 0.15,
            "window_index": index,
            "first_ten_passed": True,
            "first_ten_fills": 10,
            "first_ten_min_cash_before_entry": 10.0,
            "first_ten_ending_cash": cash,
        }

    summary = {"window_rows": [row(index) for index in range(1, 18)]}
    result = _h534_cell(summary)
    assert result["selection_all_ten"] is True
    assert result["holdout_all_ten"] is True
    assert math.isclose(result["holdout_min_cash"], 11.0)
