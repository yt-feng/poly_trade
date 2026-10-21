from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from h465_old_clean_fraction_strict_cutoff import _first_break_even_exit, _size_from_cash


def _row(**kwargs):
    values = dict(
        fill_price=0.50,
        slippage=0.005,
        side="up",
        entry_ts="2026-01-01T00:00:00Z",
        settlement_ts="2026-01-01T00:01:00Z",
        cash_release_ts="2026-01-01T00:16:00Z",
        shares=10.0,
        cost=5.0,
        fee=0.175,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def _quote(ts: str, bid: int = 60, size: float = 100) -> pd.DataFrame:
    return pd.DataFrame([{
        "ts_iso": pd.Timestamp(ts),
        "sell_up_cents": bid,
        "sell_down_cents": bid,
        "sell_up_size": size,
        "sell_down_size": size,
    }])


def test_full_cash_sizing_respects_fee_and_minimums():
    terms = _size_from_cash(_row(), 10.0)
    assert terms is not None
    assert terms["shares"] >= 5.0
    assert terms["reserved"] <= 10.0
    assert _size_from_cash(_row(), 4.99) is None


def test_strict_cutoff_rejects_depth_at_or_after_market_end_minus_60s():
    row = _row()
    assert _first_break_even_exit(row, _quote("2026-01-01T00:05:00Z")) is None
    assert _first_break_even_exit(row, _quote("2026-01-01T00:04:59Z")) is not None
