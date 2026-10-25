"""Prefix-only inputs and retrospective chronological splits for cloud research.

Feature admission never sees a future completeness test or a label.  The old
archive's first BTC sample within five seconds after expiry is an explicitly
estimated label, not an official market resolution. Raw archive reads belong
on cloud workers.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

try:
    from . import all_monthly_clob_systematic_research_v2 as base
except ImportError:
    import all_monthly_clob_systematic_research_v2 as base


FEATURES = tuple(name for name in base.LOGIT_FEATURES if name != "btc_move_4m")
PREFIX_SECONDS = 120.0
DECISION_SECONDS = 124.0
MARKET_SECONDS = 300.0
RELEASE_DELAY_SECONDS = 900.0
LABEL_BASIS = "first_close_time_btc_sample_proxy_not_official_resolution"
PREFIX_NUMERIC = tuple(base.REQUIRED_NUMERIC)
IDENTITY_COLUMNS = (
    "market_id", "run_name", "slug", "first_quote_ts", "feature_cutoff_ts",
    "decision_ts", "entry_ts", "market_start_ts", "market_close_ts",
    "settlement_ts", "cash_release_ts", "snapshot_age_seconds_1m",
    "snapshot_age_seconds_2m", "path_observation_count_first2m",
)
LABEL_COLUMNS = (
    "outcome_up", "label_available_ts", "label_source_ts", "label_basis",
    "label_availability_is_estimated", "label_source_before_market_close",
    "label_status",
)


def _ratio(left: float, right: float) -> float:
    return (left - right) / (left + right) if left + right > 0 else float("nan")


def prefix_features(
    group: pd.DataFrame, *, run_name: str, slug: str,
    max_snapshot_age_seconds: float = 90.0,
) -> tuple[dict[str, Any] | None, str]:
    """Derive one row from the first 120 seconds, excluding later observations."""
    if not np.isfinite(max_snapshot_age_seconds) or max_snapshot_age_seconds < 0:
        raise ValueError("invalid_snapshot_age_limit")
    if group.empty or "ts_utc" not in group:
        return None, "empty_market"
    group = group.sort_values("ts_utc", kind="mergesort")
    observed = group.loc[group["ts_utc"].notna()]
    if observed.empty:
        return None, "missing_first_quote_clock"
    first_ts = pd.Timestamp(observed.iloc[0]["ts_utc"])
    match = re.fullmatch(r"btc-updown-5m-(\d+)", str(slug))
    if match is None:
        return None, "invalid_market_slug"
    try:
        start = pd.to_datetime(int(match.group(1)), unit="s", utc=True)
    except (ValueError, OverflowError):
        return None, "invalid_market_epoch"
    close = start + pd.Timedelta(seconds=MARKET_SECONDS)
    cutoff = first_ts + pd.Timedelta(seconds=PREFIX_SECONDS)
    decision = first_ts + pd.Timedelta(seconds=DECISION_SECONDS)
    if first_ts < start or decision >= close:
        return None, "decision_outside_market_lifetime"
    prefix = observed.loc[observed["ts_utc"].le(cutoff)].copy()
    if len(prefix) < 3:
        return None, "too_few_prefix_quotes"
    if "quote_conflict" in prefix and prefix["quote_conflict"].any():
        return None, "conflicting_prefix_quotes"
    if "original_prefix_incomplete" in prefix and prefix["original_prefix_incomplete"].any():
        return None, "incomplete_original_prefix"
    if not set(PREFIX_NUMERIC).issubset(prefix.columns):
        return None, "missing_prefix_columns"
    numeric = prefix.loc[:, list(PREFIX_NUMERIC)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        return None, "incomplete_or_nonfinite_prefix"
    for side in ("up", "down"):
        bid, ask = numeric[f"sell_{side}_cents"], numeric[f"buy_{side}_cents"]
        if not ((bid > 0) & (bid <= ask) & (ask < 100)).all():
            return None, "invalid_prefix_book_prices"
    nonnegative = [name for name in PREFIX_NUMERIC if any(
        term in name for term in ("_size", "_depth_", "spread_", "trade_count", "trade_volume")
    )]
    if numeric[nonnegative].lt(0).any().any():
        return None, "negative_prefix_depth_spread_or_flow"
    if numeric[["target_price", "final_price"]].le(0).any().any():
        return None, "invalid_prefix_btc_price"
    if numeric["target_price"].nunique() != 1:
        return None, "changing_prefix_target"
    prefix.loc[:, list(PREFIX_NUMERIC)] = numeric
    snapshots, ages = [], []
    for offset in (60.0, 120.0):
        at = first_ts + pd.Timedelta(seconds=offset)
        row = prefix.loc[prefix["ts_utc"].le(at)].iloc[-1]
        age = float((at - row["ts_utc"]).total_seconds())
        if age > max_snapshot_age_seconds:
            return None, "stale_prefix_snapshot"
        snapshots.append(row)
        ages.append(age)
    row1, row2 = snapshots
    target = float(prefix.iloc[0]["target_price"])
    path = prefix["final_price"].astype(float)
    differences = path.diff().dropna()
    total_abs = float(differences.abs().sum())
    if total_abs <= 0:
        return None, "undefined_prefix_path_efficiency"
    move = path - target
    buy_up, buy_down = float(row2["buy_up_size"]), float(row2["buy_down_size"])
    sell_up, sell_down = float(row2["sell_up_size"]), float(row2["sell_down_size"])
    bid_up, bid_down = float(row2["bid_depth_up_5"]), float(row2["bid_depth_down_5"])
    mid_open = float(prefix.iloc[0]["mid_up_cents"]) / 100
    mid_now = float(row2["mid_up_cents"]) / 100
    record = {
        "market_id": run_name + "::" + slug, "run_name": run_name, "slug": slug,
        "first_quote_ts": first_ts, "feature_cutoff_ts": cutoff,
        "decision_ts": decision, "entry_ts": decision,
        "market_start_ts": start, "market_close_ts": close, "settlement_ts": close,
        "cash_release_ts": close + pd.Timedelta(seconds=RELEASE_DELAY_SECONDS),
        "snapshot_age_seconds_1m": ages[0], "snapshot_age_seconds_2m": ages[1],
        "path_observation_count_first2m": len(path),
        "btc_move_1m": float(row1["final_price"]) - target,
        "btc_move_2m": float(row2["final_price"]) - target,
        "mid_up_prob_open": mid_open, "mid_up_prob_2m": mid_now,
        "mid_up_prob_change_2m": mid_now - mid_open,
        "buy_up_price_2m": float(row2["buy_up_cents"]) / 100,
        "buy_down_price_2m": float(row2["buy_down_cents"]) / 100,
        "buy_up_size_2m": buy_up, "buy_down_size_2m": buy_down,
        "sell_up_size_2m": sell_up, "sell_down_size_2m": sell_down,
        "size_imbalance_updown_2m": _ratio(buy_up, buy_down),
        "sell_size_imbalance_updown_2m": _ratio(sell_up, sell_down),
        "bid_depth_imbalance_updown_2m": _ratio(bid_up, bid_down),
        "book_pressure_up_2m": _ratio(bid_up, float(row2["ask_depth_up_5"])),
        "book_pressure_down_2m": _ratio(bid_down, float(row2["ask_depth_down_5"])),
        "spread_up_median_first2m": float(prefix["spread_up_cents"].median()) / 100,
        "spread_down_median_first2m": float(prefix["spread_down_cents"].median()) / 100,
        "overround_median_first2m": float((prefix["mid_up_cents"] + prefix["mid_down_cents"] - 100).median()) / 100,
        "quote_count_first2m": len(prefix),
        "trade_count_sum_first2m": float(prefix["trade_count_1s"].sum()),
        "trade_volume_sum_first2m": float(prefix["trade_volume_1s"].sum()),
        "realized_vol_first2m": float(differences.std()),
        "path_efficiency_first2m": abs(float(path.iloc[-1] - path.iloc[0])) / total_abs,
        "max_drawdown_first2m": abs(float(move.min())),
        "max_rebound_first2m": float(move.max()),
    }
    if not np.isfinite([record[name] for name in FEATURES]).all():
        return None, "nonfinite_registered_feature"
    return record, "admitted_prefix_only"


def proxy_label(
    group: pd.DataFrame, feature: dict[str, Any], *, max_label_delay_seconds: float = 5.0,
) -> dict[str, Any]:
    """Attach only a close-time sample proxy; missing close evidence stays null."""
    if not np.isfinite(max_label_delay_seconds) or max_label_delay_seconds < 0:
        raise ValueError("invalid_label_delay_limit")
    result = {"outcome_up": float("nan"), "label_available_ts": pd.NaT,
        "label_source_ts": pd.NaT, "label_basis": LABEL_BASIS,
        "label_availability_is_estimated": True,
        "label_source_before_market_close": None, "label_status": "unknown_no_close_time_sample"}
    close = pd.Timestamp(feature["market_close_ts"])
    last_allowed = close + pd.Timedelta(seconds=max_label_delay_seconds)
    valid = group.loc[group["ts_utc"].between(close, last_allowed)].sort_values("ts_utc", kind="mergesort")
    prices = pd.to_numeric(valid.get("final_price"), errors="coerce")
    valid = valid.loc[np.isfinite(prices) & prices.gt(0)]
    if valid.empty:
        return result
    row = valid.iloc[0]
    if bool(row.get("quote_conflict", False)):
        result["label_status"] = "unknown_conflicting_close_time_sample"
        return result
    # The target is reconstructed only from the feature prefix.  A later target
    # replacement cannot change the training target used with this feature row.
    at = group.loc[group["ts_utc"].eq(feature["first_quote_ts"])].iloc[0]
    target = float(at["target_price"])
    observed = pd.Timestamp(row["ts_utc"])
    result.update(outcome_up=float(float(row["final_price"]) >= target),
        label_available_ts=observed, label_source_ts=observed,
        label_source_before_market_close=False,
        label_status="estimated_close_time_sample_proxy")
    return result


def build_from_quotes(
    quotes: pd.DataFrame, *, max_snapshot_age_seconds: float = 90.0,
    max_label_delay_seconds: float = 5.0,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, Any]]:
    """Build deterministic, duplicate-free rows from already prepared quotes."""
    required = {"run_name", "slug", "ts_utc"}
    if not required.issubset(quotes.columns):
        raise ValueError("missing_quote_identity")
    reasons: Counter = Counter()
    label_reasons: Counter = Counter()
    starts = quotes.groupby(["run_name", "slug"], dropna=False)["ts_utc"].min().reset_index()
    starts = starts.sort_values(["ts_utc", "run_name", "slug"], kind="mergesort", na_position="last")
    groups = quotes.groupby(["run_name", "slug"], dropna=False, sort=False)
    seen: set[str] = set()
    rows, books = [], {}
    for item in starts.itertuples(index=False):
        run_name, slug = str(item.run_name), str(item.slug)
        if slug in seen:
            reasons["duplicate_slug_later_occurrence"] += 1
            continue
        # Register the first occurrence before its quality verdict.  A later
        # complete copy cannot rescue an unavailable first decision prefix.
        seen.add(slug)
        group = groups.get_group((item.run_name, item.slug)).sort_values("ts_utc", kind="mergesort")
        feature, reason = prefix_features(group, run_name=run_name, slug=slug,
            max_snapshot_age_seconds=max_snapshot_age_seconds)
        reasons[reason] += 1
        if feature is None:
            continue
        label = proxy_label(group, feature, max_label_delay_seconds=max_label_delay_seconds)
        label_reasons[label["label_status"]] += 1
        row = {**feature, **label}
        rows.append(row)
        book = group.copy()
        book["ts_iso"] = book["ts_utc"]
        books[slug] = book
    frame = pd.DataFrame(rows, columns=[*IDENTITY_COLUMNS, *FEATURES, *LABEL_COLUMNS])
    if not frame.empty:
        frame = frame.sort_values(["decision_ts", "run_name", "slug"], kind="mergesort").reset_index(drop=True)
    metadata = {"format": "causal-prefix-dataset-v1", "input_market_occurrences": len(starts),
        "unique_slugs_seen": len(seen), "feature_rows": len(frame), "quality_counts": dict(reasons),
        "label_quality_counts": dict(label_reasons), "max_label_delay_seconds": max_label_delay_seconds,
        "features": list(FEATURES), "feature_cutoff_seconds": PREFIX_SECONDS,
        "decision_seconds_after_first_quote": DECISION_SECONDS,
        "future_completeness_used_for_admission": False,
        "labels_used_for_admission": False, "label_basis": LABEL_BASIS,
        "proxy_label_comparator": "gte", "proxy_equivalent_to_official_twap": False,
        "label_availability_is_estimated": True, "official_resolution_verified": False,
        "duplicate_policy": "first_chronological_occurrence_before_quality_gate",
        "missing_features_imputed": False, "holdout_evidence_type": "retrospective_reused_old_archive"}
    return frame, books, metadata


def prepare_quotes_preserving_missingness(raw: pd.DataFrame) -> pd.DataFrame:
    """Record original missingness before the legacy midpoint fallback can act."""
    marked = raw.copy(deep=False)
    original_numeric = marked.reindex(columns=PREFIX_NUMERIC).apply(pd.to_numeric, errors="coerce")
    marked["original_prefix_incomplete"] = ~np.isfinite(original_numeric.to_numpy(dtype=float)).all(axis=1)
    del original_numeric
    return base.prepare_quotes(marked)


def build_dataset(
    source_root: Path, *, max_source_bytes: int = 1_100_000_000,
    max_snapshot_age_seconds: float = 90.0,
    max_label_delay_seconds: float = 5.0,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, Any]]:
    """Cloud-only archive loading; bound and record input bytes before loading."""
    files = sorted(path for run in base.scan_run_dirs(Path(source_root)) for path in run.glob(base.FILE_GLOB))
    sizes = [path.stat().st_size for path in files]
    total = sum(sizes)
    if not files or total > max_source_bytes:
        raise ValueError("source_empty_or_byte_budget_exceeded")
    # The measured pinned archive is below 0.90 GB.  This is a cloud-only load;
    # the metadata budget is enforced before allocating its full in-memory form.
    raw, coverage = base.read_all_monthly_runs(Path(source_root))
    raw["quote_conflict"] = False
    keys = ["run_name", "slug", "ts_iso"]
    repeated = raw.duplicated(keys, keep=False)
    if repeated.any():
        dup = raw.loc[repeated].copy()
        present = [field for field in PREFIX_NUMERIC if field in dup]
        conflict = dup.groupby(keys, dropna=False)[present].transform(lambda values: values.nunique(dropna=False) > 1).any(axis=1)
        raw.loc[dup.index, "quote_conflict"] = conflict
    quotes = prepare_quotes_preserving_missingness(raw)
    raw_rows = len(raw)
    del raw
    frame, books, metadata = build_from_quotes(quotes, max_snapshot_age_seconds=max_snapshot_age_seconds,
        max_label_delay_seconds=max_label_delay_seconds)
    metadata.update(source_bytes=total, source_file_count=len(files), source_raw_rows=raw_rows,
        source_run_count=len(coverage), source_budget_bytes=max_source_bytes,
        admission_run_file_count_filter_used=False)
    return frame, books, metadata


def chronological_split(
    frame: pd.DataFrame, *, minimum_training_runs: int = 4, selection_runs: int = 20,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Freeze one training prefix and split disjoint chronological scoring runs.

Overlapping run intervals are rejected rather than shuffled across boundaries.
Unavailable labels remove training examples only, never scoring opportunities.
"""
    if minimum_training_runs < 1 or selection_runs < 1:
        raise ValueError("invalid_split_counts")
    if frame.empty:
        raise ValueError("no_prefix_rows_for_split")
    ranges = frame.groupby("run_name")["decision_ts"].agg(["min", "max"]).sort_values(["min"], kind="mergesort")
    run_order = list(ranges.index.astype(str))
    if len(run_order) <= minimum_training_runs + selection_runs:
        raise ValueError("insufficient_runs_for_registered_split")
    for index in range(1, len(ranges)):
        if ranges.iloc[index]["min"] <= ranges.iloc[:index]["max"].max():
            raise ValueError("overlapping_run_intervals_require_explicit_partition")
    training_names = run_order[:minimum_training_runs]
    selection_names = run_order[minimum_training_runs:minimum_training_runs + selection_runs]
    holdout_names = run_order[minimum_training_runs + selection_runs:]
    selection = frame.loc[frame["run_name"].isin(selection_names)].copy()
    holdout = frame.loc[frame["run_name"].isin(holdout_names)].copy()
    fit_cutoff = selection["decision_ts"].min()
    train_scope = frame.loc[frame["run_name"].isin(training_names)]
    known = train_scope["outcome_up"].isin([0.0, 1.0]) & train_scope["label_available_ts"].notna()
    train = train_scope.loc[known & train_scope["label_available_ts"].lt(fit_cutoff)].copy()
    if set(selection["slug"]) & set(holdout["slug"]):
        raise ValueError("split_slug_overlap")
    if selection["decision_ts"].max() >= holdout["decision_ts"].min():
        raise ValueError("split_time_overlap")
    receipt = {"training_runs": training_names, "selection_runs": selection_names,
        "holdout_runs": holdout_names, "run_order": run_order,
        "fit_cutoff_ts": fit_cutoff.isoformat(), "training_scope_rows": len(train_scope),
        "known_mature_training_rows": len(train), "selection_rows": len(selection),
        "holdout_rows": len(holdout), "disjoint_slugs": True, "disjoint_time_ranges": True,
        "training_label_max_ts": train["label_available_ts"].max().isoformat() if not train.empty else None,
        "fit_once_before_selection_and_holdout": True, "model_refit_during_scoring": False,
        "label_availability_is_estimated": True,
        "holdout_evidence_type": "retrospective_reused_old_archive_not_fresh_independent_holdout"}
    return train, selection, holdout, receipt
