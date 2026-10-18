import pandas as pd
import pytest

from analysis.causal_path_efficiency_threshold_grid import (
    chronological_blocks,
    gated,
    select_threshold,
)


def test_chronological_blocks_cover_runs_without_overlap():
    runs = [f"r{i}" for i in range(9)]
    blocks = chronological_blocks(runs, (2, 3, 4))
    assert blocks == [["r0", "r1"], ["r2", "r3", "r4"], ["r5", "r6", "r7", "r8"]]
    assert len({item for block in blocks for item in block}) == len(runs)


def test_chronological_blocks_reject_wrong_coverage():
    with pytest.raises(ValueError, match="BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY"):
        chronological_blocks(["r0"], (2,))


def test_gated_threshold_skips_only_low_path_rows():
    frame = pd.DataFrame(
        {
            "path_efficiency_first2m": [0.49, 0.50, 0.75],
            "v1_adaptive_mix_side": ["buy_up", "buy_down", "buy_up"],
            "v1_adaptive_mix_minute": [1, 2, 4],
            "v1_adaptive_mix_fraction": [0.25, 0.25, 0.25],
        }
    )
    result = gated(frame, 0.50)
    assert result["v1_adaptive_mix_side"].tolist() == ["skip", "buy_down", "buy_up"]
    assert result["v1_adaptive_mix_minute"].tolist() == [-1, 2, 4]


def test_selection_threshold_is_chosen_without_holdout_rows():
    rows = []
    for threshold, cash in ((0.35, 8.0), (0.50, 12.0)):
        rows.append({"threshold": threshold, "split": "selection20", "slippage": 0.005, "trades": 10, "ending_cash": cash, "net_return": cash / 10 - 1})
        rows.append({"threshold": threshold, "split": "selection20", "slippage": 0.010, "trades": 10, "ending_cash": cash, "net_return": cash / 10 - 1})
        rows.append({"threshold": threshold, "split": "selection20", "slippage": 0.015, "trades": 10, "ending_cash": cash, "net_return": cash / 10 - 1})
        for index in range(1, 5):
            for slip in (0.005, 0.010, 0.015):
                rows.append({"threshold": threshold, "split": f"selection_block{index}", "slippage": slip, "trades": 3, "ending_cash": cash, "net_return": cash / 10 - 1})
        rows.append({"threshold": threshold, "split": "holdout59", "slippage": 0.005, "trades": 10, "ending_cash": 1.0, "net_return": -0.9})
        for index in range(1, 6):
            for slip in (0.005, 0.010, 0.015):
                rows.append({"threshold": threshold, "split": f"holdout_block{index}", "slippage": slip, "trades": 2, "ending_cash": 1.0, "net_return": -0.9})
    result = select_threshold(pd.DataFrame(rows))
    assert result["selected_threshold"] == 0.50
    assert result["selection_gate_found"] is True
