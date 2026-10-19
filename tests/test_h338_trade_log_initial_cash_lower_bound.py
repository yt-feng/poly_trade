import pandas as pd

from analysis.h338_trade_log_initial_cash_lower_bound import diagnose


def row(index: int, reserved: float = 5.0, payout: float = 6.0):
    return {
        "market_id": f"market-{index}",
        "first_quote_ts": f"2026-01-01T00:{index:02d}:00Z",
        "release_ts": f"2026-01-01T00:{index + 1:02d}:00Z",
        "reserved_cash": reserved,
        "payout_at_settlement": payout,
        "strategy": "test",
    }


def test_lower_bound_accounts_for_release_and_minimum_cash(tmp_path):
    path = tmp_path / "trades.csv"
    pd.DataFrame([row(i) for i in range(10)]).to_csv(path, index=False)
    result = diagnose([path])
    assert result["eligible_files"] == 1
    assert result["eligible_at_10_usd"] == 1
    assert result["required_initial_cash_min_usd"] == 10.0


def test_lower_bound_exposes_a_cash_gap(tmp_path):
    path = tmp_path / "trades.csv"
    pd.DataFrame([row(i, reserved=5.2, payout=0.0) for i in range(10)]).to_csv(path, index=False)
    result = diagnose([path])
    assert result["eligible_at_10_usd"] == 0
    assert result["required_initial_cash_min_usd"] == 57.0
    assert result["decision"] == "ten_fill_cash_floor_requires_more_than_10_usd_on_all_observed_paths"


def test_short_log_is_skipped(tmp_path):
    path = tmp_path / "short.csv"
    pd.DataFrame([row(i) for i in range(9)]).to_csv(path, index=False)
    try:
        diagnose([path])
    except ValueError as exc:
        assert str(exc) == "NO_ELIGIBLE_TRADE_LOGS"
    else:
        raise AssertionError("expected no eligible logs")
