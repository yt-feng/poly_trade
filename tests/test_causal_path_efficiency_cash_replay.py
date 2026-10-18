import pandas as pd

from analysis.causal_path_efficiency_cash_replay import paper_prefix_summary


def _row(index: int) -> dict[str, object]:
    entry = pd.Timestamp("2026-06-01T00:00:00Z") + pd.Timedelta(minutes=20 * index)
    return {
        "first_quote_ts": entry,
        "release_ts": entry + pd.Timedelta(minutes=15),
        "filled_cost": "5",
        "fee_usd": "0.05",
        "payout_at_settlement": "6.5",
    }


def test_prefix_summary_uses_one_evolving_cash_ledger():
    result = paper_prefix_summary(pd.DataFrame([_row(index) for index in range(10)]))

    assert result["fills"] == 10
    assert result["paper_replay_passed"] is True
    assert result["ending_cash"] == "24.50"


def test_prefix_summary_rejects_fewer_than_ten_rows():
    result = paper_prefix_summary(pd.DataFrame([_row(index) for index in range(9)]))

    assert result["fills"] == 9
    assert result["paper_replay_passed"] is False
    assert result["failure_reason"] == "fewer_than_required_consecutive_fills"
