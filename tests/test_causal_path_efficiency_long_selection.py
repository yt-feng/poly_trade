import pandas as pd

from analysis.causal_path_efficiency_long_selection import compound_summary, select_threshold


def test_compound_summary_requires_ten_events_on_one_ledger():
    rows = []
    for i in range(10):
        rows.append(
            {
                "first_quote_ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=i * 20),
                "release_ts": pd.Timestamp("2026-01-01T00:10:00Z") + pd.Timedelta(minutes=i * 20),
                "filled_cost": 5.0,
                "fee_usd": 0.0,
                "payout_at_settlement": 5.5,
            }
        )
    result = compound_summary(pd.DataFrame(rows))
    assert result["fills"] == 10
    assert result["paper_replay_passed"] is True
    assert result["ending_cash"] == 15.0


def test_selection_threshold_uses_only_long_selection_rows():
    rows = []
    for threshold, cash in ((0.50, 12.0), (0.65, 14.0)):
        for slip in (0.005, 0.010, 0.015):
            rows.append({"threshold": threshold, "split": "selection40", "slippage": slip, "trades": 10, "ending_cash": cash, "net_return": cash / 10 - 1, "compound_paper_replay_passed": True, "compound_ending_cash": cash})
            for index in range(1, 6):
                rows.append({"threshold": threshold, "split": f"selection_block{index}", "slippage": slip, "trades": 2, "ending_cash": cash, "net_return": cash / 10 - 1, "compound_paper_replay_passed": True, "compound_ending_cash": cash})
            rows.append({"threshold": threshold, "split": "holdout39", "slippage": slip, "trades": 10, "ending_cash": 1.0, "net_return": -0.9, "compound_paper_replay_passed": False, "compound_ending_cash": 1.0})
            for index in range(1, 6):
                rows.append({"threshold": threshold, "split": f"holdout_block{index}", "slippage": slip, "trades": 2, "ending_cash": 1.0, "net_return": -0.9, "compound_paper_replay_passed": False, "compound_ending_cash": 1.0})
    result = select_threshold(pd.DataFrame(rows))
    assert result["selected_threshold"] == 0.65
    assert result["selection_gate_found"] is True
