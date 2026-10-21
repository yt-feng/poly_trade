import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h452_old_clean_cash_fraction import _replay_fraction, _size_from_cash


def row(**kwargs):
    defaults = dict(
        fill_price=0.50,
        slippage=0.005,
        side="up",
        entry_ts="2026-01-01T00:00:00Z",
        settlement_ts="2026-01-01T00:01:00Z",
        cash_release_ts="2026-01-01T00:16:00Z",
        run_name="r1",
        slug="m1",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def quote(ts="2026-01-01T00:00:10Z", bid=60, size=100):
    import pandas as pd

    return pd.DataFrame(
        [{"ts_iso": pd.Timestamp(ts), "sell_up_cents": bid, "sell_down_cents": bid, "sell_up_size": size, "sell_down_size": size}]
    )


def test_cash_fraction_respects_minimum_order_and_fee():
    terms = _size_from_cash(row(), 10.0, 0.5)
    assert terms is not None
    assert terms["shares"] >= 5.0
    assert terms["reserved"] <= 10.0
    assert _size_from_cash(row(), 4.99, 0.5) is None


def test_replay_requires_full_size_exit_and_uses_one_cash_ledger():
    first = row()
    second = row(
        slug="m2",
        run_name="r2",
        entry_ts="2026-01-01T00:20:00Z",
        settlement_ts="2026-01-01T00:21:00Z",
        cash_release_ts="2026-01-01T00:36:00Z",
    )
    result = _replay_fraction(
        __import__("pandas").DataFrame([first.__dict__, second.__dict__]),
        ["r1", "r2"],
        {"m1": quote(), "m2": quote("2026-01-01T00:20:10Z")},
        0.5,
        exit_cache={},
    )
    assert result["fills"] == 2
    assert result["first_ten_passed"] is False
    assert result["failure_reason"] == "fewer_than_required_consecutive_fills"


def test_replay_fails_closed_when_no_early_exit_exists():
    first = row()
    result = _replay_fraction(
        __import__("pandas").DataFrame([first.__dict__]),
        ["r1"],
        {"m1": quote(bid=40)},
        0.5,
        exit_cache={},
    )
    assert result["fills"] == 0
    assert result["failure_reason"] == "no_full_size_break_even_exit_before_30s_cutoff"
