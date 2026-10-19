import pandas as pd

from analysis.h337_old_clean_capital_floor_diagnostic import diagnose


def row(split: str, fills: int, cash: float, passed: bool):
    return {
        "split": split,
        "candidate": "candidate-a",
        "slippage": 0.005,
        "depth_floor": 100.0,
        "cash_release_delay_seconds": 600.0,
        "fee_mode": "polymarket_taker",
        "fee_rate": 0.07,
        "min_order_notional": 5.0,
        "min_order_shares": 5.0,
        "settlement_seconds": 300.0,
        "first_ten_fills": fills,
        "first_ten_minimum_cash_usd": cash,
        "first_ten_passed": passed,
        "paper_chain_fills": fills,
        "paper_chain_minimum_cash_usd": cash,
        "paper_chain_passed": passed,
        "first_ten_failure_reason": None if passed else "fewer_than_required_consecutive_fills",
        "paper_chain_failure_reason": None if passed else "fewer_than_required_consecutive_fills",
    }


def test_separates_ten_fills_from_cash_floor(tmp_path):
    path = tmp_path / "metrics.csv"
    pd.DataFrame([
        row("holdout_block_1", 10, 1.5, False),
        row("holdout_block_2", 9, 5.5, False),
    ]).to_csv(path, index=False)
    result = diagnose([path])
    assert result["contract_rows"] == 2
    assert result["first_ten_fill_rows"] == 1
    assert result["first_ten_fill_and_cash_floor_rows"] == 0
    assert result["paper_chain_fill_rows"] == 1
    assert result["paper_chain_fill_and_cash_floor_rows"] == 0
    assert result["decision"] == "capital_floor_is_binding_before_ten_fill_admission"


def test_admits_cash_floor_diagnostic_when_a_row_has_both(tmp_path):
    path = tmp_path / "metrics.csv"
    pd.DataFrame([row("holdout_block_1", 10, 5.1, True)]).to_csv(path, index=False)
    result = diagnose([path])
    assert result["first_ten_fill_and_cash_floor_rows"] == 1
    assert result["paper_chain_fill_and_cash_floor_rows"] == 1
    assert result["decision"] == "capital_floor_not_isolated"
