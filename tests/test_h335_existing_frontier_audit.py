import pandas as pd

from analysis.h335_existing_frontier_audit import audit


def row(split: str, fills: int = 10, cash: float = 5.1, passed: bool = True):
    return {
        "split": split,
        "candidate": "candidate-a",
        "slippage": 0.005,
        "depth_floor": 100.0,
        "strategy": "strategy-a",
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
    }


def test_requires_all_five_chronological_blocks(tmp_path):
    path = tmp_path / "metrics.csv"
    pd.DataFrame([row("holdout_block_1")]).to_csv(path, index=False)
    result = audit([path])
    assert result["admitted_candidates"] == []
    assert result["frontier_rows"][0]["block_count"] == 1


def test_admits_only_a_full_continuous_cash_frontier(tmp_path):
    path = tmp_path / "metrics.csv"
    pd.DataFrame([row(f"holdout_block_{i}") for i in range(1, 6)]).to_csv(path, index=False)
    result = audit([path])
    assert len(result["admitted_candidates"]) == 1
    assert result["admitted_candidates"][0]["worst_block_first_ten_fills"] == 10


def test_cash_floor_failure_rejects_even_when_each_block_has_ten_rows(tmp_path):
    path = tmp_path / "metrics.csv"
    pd.DataFrame([row(f"holdout_block_{i}", cash=4.99, passed=False) for i in range(1, 6)]).to_csv(path, index=False)
    result = audit([path])
    assert result["admitted_candidates"] == []
    assert result["decision"] == "existing_frontier_has_no_admissible_continuous_chain"
