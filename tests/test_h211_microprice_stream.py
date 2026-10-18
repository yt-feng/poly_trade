import pandas as pd

from analysis.h211_microprice_stream import make_signals, prefix_summary, replay


def test_microprice_shift_selects_the_stronger_side():
    markets = pd.DataFrame(
        [
            {
                "run_name": "r1",
                "slug": "m1",
                "entry_ts": pd.Timestamp("2026-01-01", tz="UTC"),
                "up_ask": 0.50,
                "up_size": 1000,
                "up_shift": 0.03,
                "down_ask": 0.50,
                "down_size": 1000,
                "down_shift": 0.01,
                "path_efficiency": 0.8,
                "outcome": 1.0,
            }
        ]
    )
    signals = make_signals(markets, 0.02, 0.95, False)
    assert len(signals) == 1
    assert signals.iloc[0].side == "buy_up"


def test_path_variant_is_a_separate_predeclared_filter():
    markets = pd.DataFrame(
        [
            {
                "run_name": "r1",
                "slug": "m1",
                "entry_ts": pd.Timestamp("2026-01-01", tz="UTC"),
                "up_ask": 0.50,
                "up_size": 1000,
                "up_shift": 0.03,
                "down_ask": 0.50,
                "down_size": 1000,
                "down_shift": 0.0,
                "path_efficiency": 0.2,
                "outcome": 1.0,
            }
        ]
    )
    assert make_signals(markets, 0.02, 0.95, True).empty


def test_replay_and_prefix_report_one_evolving_cash_ledger():
    rows = []
    for index in range(10):
        rows.append(
            {
                "run_name": f"r{index}",
                "slug": f"m{index}",
                "entry_ts": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(seconds=900 * index),
                "side": "buy_up",
                "ask": 0.50,
                "size": 1000,
                "shift": 0.03,
                "path_efficiency": 1.0,
                "outcome": 1.0,
            }
        )
    result = replay(pd.DataFrame(rows))
    assert result["fills"] == 10
    assert prefix_summary(result["logs"])["passed"] is True
