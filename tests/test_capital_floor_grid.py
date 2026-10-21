import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from capital_floor_grid import compound_prefix


def _logs(count=10, payout=6.0, fee=0.0):
    rows = []
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    for i in range(count):
        entry = start + pd.Timedelta(minutes=15 * i)
        rows.append(
            {
                "first_quote_ts": entry,
                "release_ts": entry + pd.Timedelta(minutes=15),
                "filled_cost": 5.0,
                "fee_usd": fee,
                "payout_at_settlement": payout,
            }
        )
    return pd.DataFrame(rows)


def test_compound_prefix_uses_one_evolving_cash_ledger():
    result = compound_prefix(_logs(), 10.0)
    assert result["fills"] == 10
    assert result["paper_replay_passed"] is True
    assert result["ending_cash"] > 10.0


def test_compound_prefix_stops_after_a_loss_breaks_minimum_order_floor():
    logs = _logs()
    logs.loc[0, "payout_at_settlement"] = 0.0
    logs.loc[0, "fee_usd"] = 0.15
    result = compound_prefix(logs, 10.0)
    assert result["fills"] == 1
    assert result["paper_replay_passed"] is False
    assert result["failure_reason"] == "insufficient_cash_for_minimum_order"
