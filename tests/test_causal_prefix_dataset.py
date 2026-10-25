from __future__ import annotations

import pandas as pd
import pytest

from analysis import causal_prefix_dataset as dataset


def quotes(*, epoch=1000, run="run0", offsets=(0, 30, 60, 90, 120, 240, 300)):
    rows = []
    for index, offset in enumerate(offsets):
        rows.append({
            "run_name": run, "slug": f"btc-updown-5m-{epoch}",
            "ts_utc": pd.to_datetime(epoch + offset, unit="s", utc=True),
            "buy_up_cents": 51.0, "sell_up_cents": 49.0,
            "buy_down_cents": 51.0, "sell_down_cents": 49.0,
            "buy_up_size": 200.0, "sell_up_size": 200.0,
            "buy_down_size": 200.0, "sell_down_size": 200.0,
            "mid_up_cents": 50.0, "mid_down_cents": 50.0,
            "spread_up_cents": 2.0, "spread_down_cents": 2.0,
            "bid_depth_up_5": 500.0, "ask_depth_up_5": 500.0,
            "bid_depth_down_5": 500.0, "ask_depth_down_5": 500.0,
            "target_price": 10000.0, "final_price": 9998.0 + index,
            "trade_count_1s": 2.0, "trade_volume_1s": 3.0,
            "quote_conflict": False,
        })
    return pd.DataFrame(rows)


def test_future_mutation_and_removal_do_not_change_admission_or_features():
    original = quotes()
    altered = original.copy()
    late = altered["ts_utc"] > pd.to_datetime(1120, unit="s", utc=True)
    altered.loc[late, ["final_price", "buy_up_cents", "trade_volume_1s"]] = float("nan")
    altered.loc[late, "quote_conflict"] = True
    before = dataset.build_from_quotes(original)[0]
    after = dataset.build_from_quotes(altered)[0]
    removed = dataset.build_from_quotes(original.loc[~late])[0]
    fields = [*dataset.IDENTITY_COLUMNS, *dataset.FEATURES]
    pd.testing.assert_frame_equal(before[fields], after[fields])
    pd.testing.assert_frame_equal(before[fields], removed[fields])
    assert len(before) == 1
    assert pd.isna(after.iloc[0]["outcome_up"])
    assert pd.isna(removed.iloc[0]["label_available_ts"])
    assert "btc_move_4m" not in dataset.FEATURES
    assert len(dataset.FEATURES) == 26


def test_nonfinite_prefix_rejected_without_imputation():
    data = quotes()
    data.loc[2, "buy_up_size"] = float("nan")
    frame, _, meta = dataset.build_from_quotes(data)
    assert frame.empty
    assert meta["quality_counts"] == {"incomplete_or_nonfinite_prefix": 1}


def test_clock_uses_actual_market_end_once():
    frame, _, _ = dataset.build_from_quotes(quotes())
    row = frame.iloc[0]
    assert row["decision_ts"] == pd.to_datetime(1124, unit="s", utc=True)
    assert row["market_close_ts"] == pd.to_datetime(1300, unit="s", utc=True)
    assert row["cash_release_ts"] == pd.to_datetime(2200, unit="s", utc=True)
    late = quotes(offsets=(180, 210, 240, 270, 300))
    assert dataset.build_from_quotes(late)[0].empty


def test_later_duplicate_does_not_rescue_rejected_first_prefix():
    first = quotes(run="first")
    first.loc[0, "buy_up_size"] = float("nan")
    later = quotes(run="second", offsets=(10, 40, 70, 100, 130, 250, 310))
    frame, _, meta = dataset.build_from_quotes(pd.concat([later, first], ignore_index=True))
    assert frame.empty
    assert meta["quality_counts"]["duplicate_slug_later_occurrence"] == 1


def test_label_clock_is_conservative_and_estimated():
    before_close = quotes(offsets=(0, 30, 60, 90, 120))
    frame, _, _ = dataset.build_from_quotes(before_close)
    row = frame.iloc[0]
    assert pd.isna(row["label_available_ts"])
    assert pd.isna(row["outcome_up"])
    assert row["label_status"] == "unknown_no_close_time_sample"
    assert row["label_availability_is_estimated"]
    assert row["label_basis"] == dataset.LABEL_BASIS
    after_close = quotes(offsets=(0, 30, 60, 90, 120, 303))
    newer = dataset.build_from_quotes(after_close)[0].iloc[0]
    assert newer["label_available_ts"] == pd.to_datetime(1303, unit="s", utc=True)
    assert not newer["label_source_before_market_close"]
    too_late = quotes(offsets=(0, 30, 60, 90, 120, 350))
    late_frame, _, meta = dataset.build_from_quotes(too_late)
    assert pd.isna(late_frame.iloc[0]["outcome_up"])
    assert meta["label_quality_counts"] == {"unknown_no_close_time_sample": 1}


def test_unrelated_late_prices_cannot_replace_close_label():
    original = quotes(offsets=(0, 30, 60, 90, 120, 300))
    tail = quotes(offsets=(1800,))
    tail["final_price"] = 1.0
    base_label = dataset.build_from_quotes(original)[0].iloc[0][list(dataset.LABEL_COLUMNS)]
    later_label = dataset.build_from_quotes(pd.concat([original, tail], ignore_index=True))[0].iloc[0][list(dataset.LABEL_COLUMNS)]
    pd.testing.assert_series_equal(base_label, later_label)


def test_close_time_proxy_exact_tie_is_up_without_claiming_official_twap():
    data = quotes()
    data.loc[data["ts_utc"].eq(pd.to_datetime(1300, unit="s", utc=True)), "final_price"] = 10000.0
    frame, _, meta = dataset.build_from_quotes(data)
    assert frame.iloc[0]["outcome_up"] == 1.0
    assert meta["proxy_label_comparator"] == "gte"
    assert meta["proxy_equivalent_to_official_twap"] is False


def test_midpoint_fallback_cannot_rescue_missing_original_prefix():
    data = quotes()
    data["ts_iso"] = data["ts_utc"].map(lambda value: value.isoformat())
    data["source_file"] = "synthetic.csv"
    data.loc[0, "mid_up_cents"] = float("nan")
    prepared = dataset.prepare_quotes_preserving_missingness(data)
    assert prepared.loc[0, "mid_up_cents"] == 50.0  # legacy derived fallback
    frame, _, meta = dataset.build_from_quotes(prepared)
    assert frame.empty
    assert meta["quality_counts"] == {"incomplete_original_prefix": 1}


def test_future_missing_midpoint_does_not_remove_prefix():
    data = quotes()
    data["ts_iso"] = data["ts_utc"].map(lambda value: value.isoformat())
    data["source_file"] = "synthetic.csv"
    data.loc[data.index[-1], "mid_up_cents"] = float("nan")
    frame, _, _ = dataset.build_from_quotes(dataset.prepare_quotes_preserving_missingness(data))
    assert len(frame) == 1


def test_frozen_split_filters_only_mature_training_labels():
    raw = pd.concat([quotes(epoch=1000 + i * 600, run=f"run{i}") for i in range(5)], ignore_index=True)
    frame, _, _ = dataset.build_from_quotes(raw)
    # A future label invalidates this training row, but absent scoring labels do
    # not erase an opportunity from the retrospective test stream.
    frame.loc[0, "label_available_ts"] = pd.to_datetime(99999, unit="s", utc=True)
    frame.loc[3, ["outcome_up", "label_available_ts"]] = [float("nan"), pd.NaT]
    train, selection, holdout, receipt = dataset.chronological_split(frame, minimum_training_runs=2, selection_runs=1)
    assert list(train["run_name"]) == ["run1"]
    assert list(selection["run_name"]) == ["run2"]
    assert list(holdout["run_name"]) == ["run3", "run4"]
    assert receipt["disjoint_slugs"] and receipt["disjoint_time_ranges"]
    assert receipt["model_refit_during_scoring"] is False


def test_overlapping_run_intervals_rejected():
    raw = pd.concat([quotes(epoch=1000 + i * 600, run=f"run{i}") for i in range(5)], ignore_index=True)
    frame, _, _ = dataset.build_from_quotes(raw)
    frame.loc[4, "run_name"] = "run0"
    with pytest.raises(ValueError, match="overlapping_run_intervals"):
        dataset.chronological_split(frame, minimum_training_runs=1, selection_runs=1)
