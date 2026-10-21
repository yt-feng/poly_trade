import pandas as pd

from analysis.all_monthly_clob_systematic_research_v2 import build_features, prepare_quotes


FIELDS = {
    "buy_up_cents": 55,
    "buy_down_cents": 45,
    "sell_up_cents": 56,
    "sell_down_cents": 46,
    "buy_up_size": 200,
    "buy_down_size": 200,
    "sell_up_size": 200,
    "sell_down_size": 200,
    "spread_up_cents": 1,
    "spread_down_cents": 1,
    "bid_depth_up_5": 200,
    "ask_depth_up_5": 200,
    "bid_depth_down_5": 200,
    "ask_depth_down_5": 200,
}


def _quotes(*, missing_minute=None, one_path_point=False):
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    minutes = [0, 1, 2, 4]
    if missing_minute is not None:
        minutes.remove(missing_minute)
    rows = []
    for minute in minutes:
        row = {
            "ts_iso": (start + pd.Timedelta(minutes=minute)).isoformat(),
            "slug": "btc-updown-5m-1767225600",
            "run_name": "run-a",
            "source_file": "quotes.csv",
            "target_price": 100.0,
            "final_price": 100.0 + (0.5 if minute else 0.0),
        }
        row.update(FIELDS)
        rows.append(row)
    if one_path_point:
        for row in rows:
            row["final_price"] = None
        rows[0]["final_price"] = 100.0
    return pd.DataFrame(rows)


def test_strict_quality_accepts_complete_bounded_snapshots():
    out = build_features(prepare_quotes(_quotes()), strict_quality=True)
    assert len(out) == 1
    assert out.iloc[0]["path_observation_count_first2m"] == 3
    assert out.iloc[0]["snapshot_age_seconds_4m"] == 0.0


def test_strict_quality_rejects_missing_requested_snapshot():
    assert build_features(prepare_quotes(_quotes(missing_minute=4)), strict_quality=True).empty


def test_strict_quality_rejects_insufficient_path_instead_of_promoting_one_point_to_one():
    assert build_features(prepare_quotes(_quotes(one_path_point=True)), strict_quality=True).empty


def test_legacy_mode_remains_reproducible_for_existing_replays():
    out = build_features(prepare_quotes(_quotes(missing_minute=4)), strict_quality=False)
    assert len(out) == 1
    assert out.iloc[0]["path_efficiency_first2m"] == 1.0


def test_prepare_quotes_vectorizes_close_timestamp_and_keeps_invalid_slug_as_nat():
    frame = _quotes().copy()
    frame.loc[0, "slug"] = "not-a-btc-window"
    out = prepare_quotes(frame)
    valid = out.loc[out["slug"].eq("btc-updown-5m-1767225600"), "close_ts_utc"]
    invalid = out.loc[out["slug"].eq("not-a-btc-window"), "close_ts_utc"]
    assert valid.iloc[0] == pd.Timestamp("2026-01-01T00:00:00Z")
    assert invalid.isna().all()
