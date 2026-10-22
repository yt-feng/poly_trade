from analysis.h651_h159_fixed_holdout import FIXED_STRATEGY, _gate


def test_h651_strategy_is_the_frozen_h159_selection_best():
    assert FIXED_STRATEGY == "classic_breakout_up__fraction_0p80"


def test_h651_gate_requires_all_rows_to_be_funded_ten_fill_prefixes():
    row = {
        "first_ten_passed": True,
        "first_ten_fills": 10,
        "first_ten_ending_cash_usd": 12.0,
        "first_ten_minimum_cash_usd": 1.7,
    }
    assert _gate([row, dict(row)])
    failed = dict(row)
    failed["first_ten_fills"] = 9
    assert not _gate([row, failed])
