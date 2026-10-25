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
    assert meta["prefix_nonfinite_market_counts_by_field"]["mid_up_cents"] == 1


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


def test_synthetic_csv_schema_roundtrip_keeps_valid_prefix(tmp_path):
    """Exercise the actual loader/preparer contract with seven invented rows."""
    raw = quotes(epoch=1792860000, run="will_be_set_by_loader")
    raw["ts_iso"] = raw["ts_utc"].map(lambda value: value.isoformat())
    for column in dataset.PREFIX_NUMERIC:
        raw[column] = raw[column].map(str)
    raw = raw.drop(columns=["ts_utc", "run_name", "quote_conflict"])
    run_dir = tmp_path / "run_20261024_000000"
    run_dir.mkdir()
    source = run_dir / "synthetic_btc-updown-5m_quotes.csv"
    raw.to_csv(source, index=False)
    frame, books, meta = dataset.build_dataset(tmp_path)
    assert len(frame) == 1
    assert frame.iloc[0]["run_name"] == run_dir.name
    assert set(books) == {"btc-updown-5m-1792860000"}
    assert meta["quality_counts"] == {"admitted_prefix_only": 1}
    assert meta["source_file_count"] == 1
    assert meta["source_raw_rows"] == 7
    assert meta["source_bytes"] == source.stat().st_size
    assert not any(meta["prefix_nonfinite_market_counts_by_field"].values())
    assert meta["first_quote_minus_slug_epoch_seconds_min"] == 0.0
    assert meta["first_quote_minus_slug_epoch_seconds_max"] == 0.0
    assert meta["first_quote_before_slug_epoch_market_count"] == 0


@pytest.mark.parametrize(
    "missing_column", ["mid_up_cents", "trade_count_1s", "trade_volume_1s"]
)
def test_synthetic_csv_missing_original_field_rejection_is_diagnostic(tmp_path, missing_column):
    raw = quotes(epoch=1792860000)
    raw["ts_iso"] = raw["ts_utc"].map(lambda value: value.isoformat())
    raw = raw.drop(columns=["ts_utc", "run_name", "quote_conflict", missing_column])
    run_dir = tmp_path / "run_20261024_000000"
    run_dir.mkdir()
    raw.to_csv(run_dir / "synthetic_btc-updown-5m_quotes.csv", index=False)
    frame, _, meta = dataset.build_dataset(tmp_path)
    assert frame.empty
    assert meta["quality_counts"] == {"incomplete_original_prefix": 1}
    assert meta["input_market_occurrences"] == 1
    assert meta["prefix_nonfinite_market_counts_by_field"][missing_column] == 1
    assert sum(meta["prefix_nonfinite_market_counts_by_field"].values()) == 1


def test_prefix_diagnostics_capture_clock_rejection_and_ignore_future_missingness():
    data = quotes(offsets=(-120, -90, -60, -30, 0, 180))
    data.loc[data.index[-1], "mid_up_cents"] = float("nan")
    frame, _, meta = dataset.build_from_quotes(data)
    assert frame.empty
    assert meta["quality_counts"] == {"decision_outside_market_lifetime": 1}
    assert meta["first_quote_minus_slug_epoch_seconds_min"] == -120.0
    assert meta["first_quote_minus_slug_epoch_seconds_max"] == -120.0
    assert meta["first_quote_before_slug_epoch_market_count"] == 1
    assert not any(meta["prefix_nonfinite_market_counts_by_field"].values())


def test_versioned_price_book_contract_removes_only_flow_features_and_inputs():
    full_features, full_inputs = dataset.feature_contract()
    price_features, price_inputs = dataset.feature_contract("price_book_v1")
    assert full_features == dataset.FEATURES
    assert full_inputs == dataset.PREFIX_NUMERIC
    assert set(full_features) - set(price_features) == dataset.FLOW_FEATURES
    assert set(full_inputs) - set(price_inputs) == dataset.FLOW_SOURCE_FIELDS
    assert len(price_features) == 24
    with pytest.raises(ValueError, match="unknown_feature_profile"):
        dataset.feature_contract("automatic_fallback")


def test_price_book_profile_preserves_other_feature_values_and_labels():
    raw = quotes()
    full, _, _ = dataset.build_from_quotes(raw)
    no_flow = raw.drop(columns=list(dataset.FLOW_SOURCE_FIELDS))
    reduced, _, meta = dataset.build_from_quotes(no_flow, feature_profile="price_book_v1")
    active_features, required = dataset.feature_contract("price_book_v1")
    compare = [*dataset.IDENTITY_COLUMNS, *active_features, *dataset.LABEL_COLUMNS]
    pd.testing.assert_frame_equal(full[compare], reduced[compare])
    assert set(dataset.FLOW_FEATURES).isdisjoint(reduced.columns)
    assert meta["required_numeric_fields"] == list(required)
    assert meta["feature_profile"] == "price_book_v1"
    assert meta["prefix_nonfinite_market_counts_by_field"]["trade_count_1s"] == 1
    assert dataset.build_from_quotes(no_flow)[0].empty


def test_price_book_profile_uses_required_bits_and_still_rejects_missing_target(tmp_path):
    raw = quotes(epoch=1792860000)
    raw["ts_iso"] = raw["ts_utc"].map(lambda value: value.isoformat())
    raw = raw.drop(columns=["ts_utc", "run_name", "quote_conflict", *dataset.FLOW_SOURCE_FIELDS])
    run_dir = tmp_path / "run_20261024_000000"
    run_dir.mkdir()
    source = run_dir / "synthetic_btc-updown-5m_quotes.csv"
    raw.to_csv(source, index=False)
    full, _, _ = dataset.build_dataset(tmp_path)
    reduced, _, meta = dataset.build_dataset(tmp_path, feature_profile="price_book_v1")
    assert full.empty and len(reduced) == 1
    assert meta["original_missingness_mask_fields"] == list(dataset.PREFIX_NUMERIC)
    raw.loc[0, "target_price"] = float("nan")
    raw.to_csv(source, index=False)
    rejected, _, rejected_meta = dataset.build_dataset(tmp_path, feature_profile="price_book_v1")
    assert rejected.empty
    assert rejected_meta["quality_counts"] == {"incomplete_original_prefix": 1}
