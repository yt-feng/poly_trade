import importlib.util
import sys
from pathlib import Path

import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "analysis"
    / "quantml_candidate_execution_walk_forward.py"
)
SPEC = importlib.util.spec_from_file_location("quantml_execution", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_delayed_snapshot_lookup_keeps_timestamp_units_aligned() -> None:
    first = pd.Timestamp("2026-08-04 18:40:00", tz="UTC")
    quotes = pd.DataFrame(
        {
            "market_id": ["m", "m", "m", "m"],
            "ts_utc": [
                first,
                first + pd.Timedelta(seconds=59),
                first + pd.Timedelta(minutes=1, seconds=1),
                first + pd.Timedelta(minutes=1, seconds=5),
            ],
            "buy_up_cents": [50, 51, 52, 53],
            "buy_down_cents": [50, 49, 48, 47],
            "buy_up_size": [200, 200, 200, 200],
            "buy_down_size": [200, 200, 200, 200],
            "spread_up_cents": [1, 1, 1, 1],
            "spread_down_cents": [1, 1, 1, 1],
            "mid_overround_cents": [0, 0, 0, 0],
        }
    )
    features = pd.DataFrame({"market_id": ["m"]})

    out = MODULE.attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1,))

    # Signal is the last quote at or before +1m (00:00:59); the delayed fill
    # is the first quote at or after +4s (00:01:05).
    assert out.loc[0, "exec_buy_down_1m_d4_signal_ts"] == first + pd.Timedelta(seconds=59)
    assert out.loc[0, "exec_buy_down_1m_d4_quote_ts"] == first + pd.Timedelta(minutes=1, seconds=5)
    assert out.loc[0, "exec_buy_down_1m_d4_quote_price"] == 0.47
