import pandas as pd
import pytest

from analysis.h623_h600_cash_release_sensitivity import (
    DELAYS_SECONDS,
    THRESHOLDS,
    shift_release_times,
)


def test_h623_freezes_h600_entry_and_depth_policy():
    assert THRESHOLDS == (3.0, 5.0, 7.0)
    assert DELAYS_SECONDS == (300.0, 600.0, 900.0)


def test_shift_release_times_changes_only_the_counterfactual_clock():
    frame = pd.DataFrame(
        {
            "settlement_ts": pd.to_datetime(["2026-01-01T00:05:00Z"]),
            "cash_release_ts": pd.to_datetime(["2026-01-01T00:20:00Z"]),
            "slug": ["m1"],
        }
    )
    result = shift_release_times(frame, 300.0)
    assert result.loc[0, "cash_release_ts"] == pd.Timestamp("2026-01-01T00:10:00Z")
    assert result.loc[0, "slug"] == "m1"
    assert frame.loc[0, "cash_release_ts"] == pd.Timestamp("2026-01-01T00:20:00Z")


def test_shift_release_times_rejects_unregistered_clock():
    frame = pd.DataFrame({"settlement_ts": ["2026-01-01T00:05:00Z"]})
    with pytest.raises(ValueError, match="unsupported release delay"):
        shift_release_times(frame, 450.0)

