import pandas as pd
from types import SimpleNamespace

from analysis.h506_h452_execution_depth_stress import _first_stressed_exit


def row(**overrides):
    values = {
        "side": "up",
        "entry_ts": "2026-01-01T00:00:00Z",
        "settlement_ts": "2026-01-01T00:01:00Z",
        "slippage": 0.005,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def quotes(*, ts="2026-01-01T00:00:00.500Z", size=10, bid=60):
    return pd.DataFrame(
        [
            {
                "ts_iso": pd.Timestamp(ts),
                "sell_up_cents": bid,
                "sell_up_size": size,
                "sell_down_cents": bid,
                "sell_down_size": size,
            }
        ]
    )


def test_latency_excludes_quote_before_effective_observation_time():
    assert _first_stressed_exit(
        row(), quotes(), shares=5, entry_outlay=2.0, latency_seconds=0.25, depth_haircut=1.0
    ) is not None
    assert _first_stressed_exit(
        row(), quotes(ts="2026-01-01T00:00:00.100Z"), shares=5, entry_outlay=2.0,
        latency_seconds=0.25, depth_haircut=1.0
    ) is None


def test_depth_haircut_requires_effective_size_for_full_exit():
    assert _first_stressed_exit(
        row(), quotes(size=6), shares=5, entry_outlay=2.0, latency_seconds=0.0, depth_haircut=1.0
    ) is not None
    assert _first_stressed_exit(
        row(), quotes(size=6), shares=5, entry_outlay=2.0, latency_seconds=0.0, depth_haircut=0.75
    ) is None


def test_stress_parameters_are_bounded():
    try:
        _first_stressed_exit(
            row(), quotes(), shares=5, entry_outlay=2.0, latency_seconds=-0.01, depth_haircut=1.0
        )
    except ValueError as exc:
        assert "latency_seconds" in str(exc)
    else:
        raise AssertionError("negative latency must be rejected")
