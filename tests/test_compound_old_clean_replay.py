import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from compound_old_clean_replay import _select_strategy


def test_selection_requires_growth_positive_ten_fill_chain_before_ranking_cash():
    frame = pd.DataFrame(
        [
            {
                "strategy": "high_frequency_loser",
                "paper_chain_passed": False,
                "paper_chain_fills": 50,
                "paper_chain_ending_cash": 25.0,
                "paper_chain_minimum_cash": 10.0,
            },
            {
                "strategy": "eligible_lower_cash",
                "paper_chain_passed": True,
                "paper_chain_fills": 10,
                "paper_chain_ending_cash": 11.0,
                "paper_chain_minimum_cash": 5.0,
            },
            {
                "strategy": "eligible_higher_cash",
                "paper_chain_passed": True,
                "paper_chain_fills": 10,
                "paper_chain_ending_cash": 12.0,
                "paper_chain_minimum_cash": 5.5,
            },
            {
                "strategy": "short_chain",
                "paper_chain_passed": True,
                "paper_chain_fills": 9,
                "paper_chain_ending_cash": 30.0,
                "paper_chain_minimum_cash": 10.0,
            },
        ]
    )
    selected, eligible_count = _select_strategy(frame)
    assert selected == "eligible_higher_cash"
    assert eligible_count == 2


def test_selection_returns_no_candidate_without_a_ten_fill_growth_chain():
    frame = pd.DataFrame(
        [
            {
                "strategy": "loser",
                "paper_chain_passed": False,
                "paper_chain_fills": 20,
                "paper_chain_ending_cash": 20.0,
                "paper_chain_minimum_cash": 5.0,
            },
            {
                "strategy": "short",
                "paper_chain_passed": True,
                "paper_chain_fills": 9,
                "paper_chain_ending_cash": 20.0,
                "paper_chain_minimum_cash": 5.0,
            },
        ]
    )
    assert _select_strategy(frame) == (None, 0)


def test_selection_rejects_chain_that_dips_below_minimum_free_cash():
    frame = pd.DataFrame(
        [
            {
                "strategy": "ending_cash_only_false_positive",
                "paper_chain_passed": True,
                "paper_chain_fills": 24,
                "paper_chain_ending_cash": 14.7,
                "paper_chain_minimum_cash": 0.03,
            }
        ]
    )
    assert _select_strategy(frame) == (None, 0)
