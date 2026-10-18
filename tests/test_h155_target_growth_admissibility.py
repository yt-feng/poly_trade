import math

import pandas as pd

from analysis.h155_target_growth_admissibility import (
    CANDIDATES,
    ENTRY_PRICE_CAP,
    RAW_EDGE_FLOOR,
    TARGET_MULTIPLIER,
    apply_target_gate,
    chronological_blocks,
    minimum_win_price_cap,
)


def test_target_multiplier_and_price_cap_are_frozen_and_ordered():
    assert math.isclose(TARGET_MULTIPLIER, 100 ** (1 / 10), rel_tol=1e-12)
    assert math.isclose(ENTRY_PRICE_CAP, minimum_win_price_cap(), rel_tol=1e-12)
    assert 0.44 < ENTRY_PRICE_CAP < 0.46


def test_blocks_cover_without_overlap():
    runs = [f"r{i}" for i in range(10)]
    blocks = chronological_blocks(runs, (3, 3, 4))
    assert [len(block) for block in blocks] == [3, 3, 4]
    assert [item for block in blocks for item in block] == runs


def test_target_gate_requires_entry_price_and_raw_edge():
    candidate = CANDIDATES[0]
    frame = pd.DataFrame(
        {
            "run_name": ["r1", "r1", "r1"],
            "market_id": ["m1", "m2", "m3"],
            "first_quote_ts": pd.date_range("2026-01-01", periods=3, freq="min", tz="UTC"),
            "btc_move_2m": [20.0, 20.0, 20.0],
            "mid_up_prob_2m": [0.8, 0.8, 0.8],
            "path_efficiency_first2m": [0.7, 0.7, 0.7],
            "session_et": ["x", "x", "x"],
            f"{candidate}_side": ["buy_up", "buy_up", "buy_up"],
            f"{candidate}_minute": [2, 2, 2],
            "exec_buy_up_2m_d4_quote_price": [ENTRY_PRICE_CAP - 0.01, ENTRY_PRICE_CAP - 0.01, ENTRY_PRICE_CAP + 0.01],
            "exec_buy_up_2m_d4_quote_spread": [0.01, 0.01, 0.01],
            "exec_buy_up_2m_d4_quote_size": [1000.0, 1000.0, 1000.0],
        }
    )
    # entry_table uses all executable fields only for the active side/minute.
    gated, stats = apply_target_gate(frame, candidate)
    assert stats["eligible_rows"] <= stats["signal_rows_before_gate"]
    assert set(gated[f"{candidate}_side"]) <= {"buy_up", "skip"}
    assert gated.loc[2, f"{candidate}_side"] == "skip"
    assert RAW_EDGE_FLOOR > 0
