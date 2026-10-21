import pandas as pd
from types import SimpleNamespace

from analysis.h508_cash_fraction_depth_tradeoff import _replay_fraction_depth


def row(**overrides):
    values = {
        "fill_price": 0.50,
        "slippage": 0.005,
        "side": "up",
        "entry_ts": "2026-01-01T00:00:00Z",
        "settlement_ts": "2026-01-01T00:01:00Z",
        "cash_release_ts": "2026-01-01T00:16:00Z",
        "run_name": "r1",
        "slug": "m1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def quote(ts="2026-01-01T00:00:10Z", bid=60, size=100):
    return pd.DataFrame([{
        "ts_iso": pd.Timestamp(ts),
        "sell_up_cents": bid,
        "sell_down_cents": bid,
        "sell_up_size": size,
        "sell_down_size": size,
    }])


def test_fraction_reduces_required_exit_depth_and_preserves_one_ledger():
    first = row()
    second = row(
        slug="m2", run_name="r2", entry_ts="2026-01-01T00:20:00Z",
        settlement_ts="2026-01-01T00:21:00Z", cash_release_ts="2026-01-01T00:36:00Z",
    )
    frame = pd.DataFrame([first.__dict__, second.__dict__])
    quotes = {"m1": quote(size=50), "m2": quote("2026-01-01T00:20:10Z", size=50)}
    full = _replay_fraction_depth(
        frame, ["r1", "r2"], quotes, gross_cash_fraction=1.0,
        latency_seconds=0.0, depth_haircut=0.25, exit_cache={}
    )
    partial = _replay_fraction_depth(
        frame, ["r1", "r2"], quotes, gross_cash_fraction=0.5,
        latency_seconds=0.0, depth_haircut=0.25, exit_cache={}
    )
    assert full["fills"] == 0
    assert partial["fills"] == 2
    assert partial["first_ten_passed"] is False
    assert partial["failure_reason"] == "fewer_than_required_consecutive_fills"


def test_fraction_depth_rejects_invalid_fraction():
    try:
        _replay_fraction_depth(
            pd.DataFrame([row().__dict__]), ["r1"], {"m1": quote()},
            gross_cash_fraction=0.0, latency_seconds=0.0, depth_haircut=1.0,
            exit_cache={}
        )
    except ValueError as exc:
        assert "gross_cash_fraction" in str(exc)
    else:
        raise AssertionError("zero cash fraction must be rejected")
