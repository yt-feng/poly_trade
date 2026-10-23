"""Configuration-bound retrospective policy audit on an existing archive.

The caller supplies a private protocol. Outputs belong in the sealed research
delivery; this module neither authenticates executions nor places orders.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import all_monthly_clob_systematic_research_v2 as source
import h432_old_clean_extratrees_compound as signal
import h561_online_depth_proxy as execution


def feature_available_seconds(name: str) -> float:
    if name not in source.LOGIT_FEATURES:
        raise ValueError("unregistered_feature")
    if name == "mid_up_prob_open":
        return 0.0
    for suffix, seconds in (("_1m", 60.0), ("_2m", 120.0), ("_4m", 240.0), ("first2m", 120.0)):
        if name.endswith(suffix):
            return seconds
    raise ValueError("feature_availability_unknown")


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    fields = ("protocol_id", "source_commit", "features", "decision_cutoff_seconds",
              "entry_delay_seconds", "maximum_snapshot_age_seconds", "model_params",
              "confidence_floor", "favorite_price_floor", "depth_thresholds", "slippages",
              "latencies_seconds", "depth_haircuts", "minimum_training_runs", "evaluation_block_runs",
              "terminal_proxy_max_gap_seconds", "cash_release_delay_after_close_seconds")
    if any(key not in config for key in fields):
        raise ValueError("incomplete_private_protocol")
    features = config["features"]
    if not isinstance(features, list) or not features or len(set(features)) != len(features):
        raise ValueError("invalid_feature_allowlist")
    cutoff = float(config["decision_cutoff_seconds"])
    # The inherited execution snapshot contract is specifically minute two.
    if cutoff != 120.0 or any(feature_available_seconds(name) > cutoff for name in features):
        raise ValueError("feature_not_available_at_decision")
    if float(config["entry_delay_seconds"]) < 0 or float(config["maximum_snapshot_age_seconds"]) < 0:
        raise ValueError("invalid_clock_contract")
    if not 0.5 <= float(config["confidence_floor"]) < 1.0:
        raise ValueError("invalid_confidence_floor")
    if not 0 <= float(config["favorite_price_floor"]) < 1:
        raise ValueError("invalid_price_floor")
    thresholds = list(map(float, config["depth_thresholds"]))
    if len(thresholds) != 3 or not 0 < thresholds[0] < thresholds[1] < thresholds[2]:
        raise ValueError("invalid_depth_map")
    for key in ("minimum_training_runs", "evaluation_block_runs"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError("invalid_run_partition")
    if not config["slippages"] or any(not 0 <= float(x) < 0.1 for x in config["slippages"]):
        raise ValueError("invalid_slippage")
    if not config["latencies_seconds"] or any(float(x) < 0 for x in config["latencies_seconds"]):
        raise ValueError("invalid_latency")
    if not config["depth_haircuts"] or any(not 0 < float(x) <= 1 for x in config["depth_haircuts"]):
        raise ValueError("invalid_depth_haircut")
    if not isinstance(config["model_params"], dict) or "random_state" not in config["model_params"]:
        raise ValueError("deterministic_model_required")
    if not 0 <= float(config["terminal_proxy_max_gap_seconds"]) <= 5 or float(config["cash_release_delay_after_close_seconds"]) < 0:
        raise ValueError("invalid_terminal_clock_protocol")
    return config


def prefix_quality(quotes: pd.DataFrame, maximum_age: float, terminal_gap_seconds: float = 1.0) -> pd.DataFrame:
    """Eligibility uses decision-time rows; label availability is metadata only."""
    records = []
    for market_id, group in quotes.groupby("market_id", sort=False):
        group = group.sort_values("ts_utc")
        cutoff = group["ts_utc"].min() + pd.Timedelta(seconds=120)
        prefix = group[group["ts_utc"] <= cutoff]
        gates = {"target_visible_before_decision": bool(not prefix["target_price"].dropna().empty)}
        for minute in (1, 2):
            row, age = source.snapshot_row_with_age(prefix, minute)
            gates[f"snapshot_{minute}m_complete_and_recent"] = bool(row is not None and age <= maximum_age and source._strict_snapshot_complete(row))
        gates["three_prefix_price_observations"] = int(prefix["final_price"].notna().sum()) >= 3
        path = pd.to_numeric(prefix["final_price"], errors="coerce").dropna()
        gates["prefix_path_ratio_defined"] = float(path.diff().abs().sum()) > 0
        start_epoch = pd.to_numeric(str(group.iloc[0]["slug"]).rsplit("-", 1)[-1], errors="coerce")
        close = pd.to_datetime(start_epoch, unit="s", utc=True, errors="coerce") + pd.Timedelta(seconds=300)
        gates["decision_before_market_close"] = bool(pd.notna(close) and cutoff < close)
        # The declared proxy is never available to training before market close.
        gaps = (group["ts_utc"] - close).dt.total_seconds()
        labels = group.loc[group["final_price"].notna() & gaps.abs().le(terminal_gap_seconds)].copy()
        labels["terminal_gap"] = gaps.loc[labels.index]
        if not labels.empty:
            labels = labels.assign(abs_gap=labels["terminal_gap"].abs()).sort_values(["abs_gap", "ts_utc"])
        target = source.first_non_null(prefix["target_price"])
        label = float(labels.iloc[0]["final_price"] > target) if not labels.empty and pd.notna(target) else np.nan
        records.append({"market_id": market_id, "prefix_quality_passed": all(gates.values()),
                        **{"gate_" + key: value for key, value in gates.items()},
                        "market_close_ts": close,
                        "qualified_outcome_up": label,
                        "terminal_proxy_observed_ts": labels.iloc[0]["ts_utc"] if not labels.empty else pd.NaT,
                        "terminal_proxy_gap_seconds": float(labels.iloc[0]["terminal_gap"]) if not labels.empty else np.nan,
                        "label_observed_ts": max(labels.iloc[0]["ts_utc"], close) if not labels.empty else pd.NaT})
    return pd.DataFrame(records)


def admit_feature_rows(features: pd.DataFrame, feature_names: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Reject missing registered inputs explicitly; never choose a new schema."""
    required = sorted(set(feature_names + ["buy_up_price_2m", "buy_down_price_2m", "buy_up_size_2m", "buy_down_size_2m",
        "sell_up_size_2m", "sell_down_size_2m", "spread_up_median_first2m", "spread_down_median_first2m", "overround_median_first2m"]))
    numeric = features[required].apply(pd.to_numeric, errors="coerce")
    finite_matrix = np.isfinite(numeric.to_numpy())
    finite = finite_matrix.all(axis=1)
    prefix_pass = features["prefix_quality_passed"].astype(bool).to_numpy()
    admitted = features.loc[prefix_pass & finite].copy()
    diagnostics = {"rows_before_quality": len(features), "prefix_quality_passed_rows": int(prefix_pass.sum()),
        "all_registered_inputs_finite_rows": int(finite.sum()), "prefix_and_inputs_passed_rows": len(admitted),
        "prefix_pass_but_missing_registered_inputs_rows": int((prefix_pass & ~finite).sum()),
        "nonfinite_rows_by_required_field": {name: int((~finite_matrix[:, index]).sum()) for index, name in enumerate(required)},
        "prefix_pass_nonfinite_rows_by_required_field": {name: int((prefix_pass & ~finite_matrix[:, index]).sum()) for index, name in enumerate(required)},
        "prefix_gate_rejected_rows": {name.removeprefix("gate_"): int((~features[name].astype(bool)).sum()) for name in features if name.startswith("gate_")},
        "missing_feature_policy": "reject_row_no_imputation_no_automatic_feature_dropping"}
    return admitted, diagnostics


def evaluation_status(feature_rows: int, fitted_scored_runs: int) -> tuple[str, bool]:
    if feature_rows == 0:
        return "not_evaluated_empty_quality_dataset", False
    if fitted_scored_runs == 0:
        return "not_evaluated_insufficient_training_data", False
    return "evaluated", True


def causal_training_rows(features: pd.DataFrame, previous_runs: list[str], current: pd.DataFrame) -> pd.DataFrame:
    if current.empty:
        return features.iloc[:0].copy()
    cutoff = pd.to_datetime(current["decision_ts"], utc=True).min()
    prior = features[features["run_name"].astype(str).isin(previous_runs)].copy()
    known = pd.to_datetime(prior["label_observed_ts"], utc=True, errors="coerce")
    eligible = known.lt(cutoff) & ~prior["slug"].astype(str).isin(current["slug"].astype(str))
    if "outcome_up" in prior:
        eligible &= prior["outcome_up"].notna()
    return prior.loc[eligible].copy()


def disjoint_blocks(run_names: list[str], block_runs: int) -> list[list[str]]:
    if len(set(run_names)) != len(run_names) or block_runs < 1:
        raise ValueError("invalid_unique_run_partition")
    return [run_names[start:start + block_runs] for start in range(0, len(run_names), block_runs)]


@contextmanager
def isolated_signal_contract(features: list[str], entry_seconds: float):
    old_features, old_entry = signal.FEATURES, signal.ENTRY_SECONDS_AFTER_START
    signal.FEATURES, signal.ENTRY_SECONDS_AFTER_START = list(features), float(entry_seconds)
    try:
        yield
    finally:
        signal.FEATURES, signal.ENTRY_SECONDS_AFTER_START = old_features, old_entry


def cost_checked_candidate_frame(model: Any, medians: pd.Series, current: pd.DataFrame,
                                 slippage: float) -> pd.DataFrame:
    """Apply the net-edge gate to the simulated fill price, including slippage.

    For nonnegative slippage this can only remove candidates from the legacy
    signal gate. Other signal filters remain unchanged; frozen legacy reports
    retain their original semantics.
    """
    if not np.isfinite(float(slippage)) or float(slippage) < 0:
        raise ValueError("invalid_nonnegative_slippage")
    frame = signal._candidate_frame(model, medians, current, slippage)
    if frame.empty:
        return frame
    fee_per_share = signal.TAKER_FEE_RATE * frame["fill_price"] * (1 - frame["fill_price"])
    frame = frame.copy()
    frame["edge"] = frame["q_side"] - frame["fill_price"] - fee_per_share
    return frame.loc[frame["edge"].ge(signal.EDGE_THRESHOLD)].copy()


def prepare_archive(source_root: Path, config: dict[str, Any]) -> tuple[pd.DataFrame, list[str], dict[str, Any], dict[str, pd.DataFrame]]:
    raw, coverage = source.read_all_monthly_runs(source_root)
    maximum = int(coverage["file_count"].max())
    complete = coverage.loc[coverage["file_count"].eq(maximum), "run_name"].astype(str).tolist()
    raw = raw[raw["run_name"].astype(str).isin(complete)].copy()
    duplicate_rows = raw[raw.duplicated(["run_name", "slug", "ts_iso"], keep=False)]
    conflicts = set()
    if not duplicate_rows.empty:
        compared = [c for c in source.REQUIRED_NUMERIC if c in duplicate_rows]
        counts = duplicate_rows.groupby(["run_name", "slug", "ts_iso"])[compared].nunique(dropna=False)
        for run, slug, _ in counts.index[counts.gt(1).any(axis=1)]:
            conflicts.add(str(run) + "::" + str(slug))
    quotes = source.prepare_quotes(raw)
    quotes = quotes[~quotes["market_id"].astype(str).isin(conflicts)].copy()
    del raw
    quality = prefix_quality(quotes, float(config["maximum_snapshot_age_seconds"]), float(config["terminal_proxy_max_gap_seconds"]))
    # Prefix eligibility is evaluated separately, without requiring a later snapshot.
    features = source.build_features(quotes, strict_quality=False).merge(quality, on="market_id", validate="one_to_one")
    before = len(features)
    features, admission = admit_feature_rows(features, list(config["features"]))
    features["outcome_up"] = features["qualified_outcome_up"]
    features = features.sort_values(["first_quote_ts", "run_name", "slug"], kind="mergesort")
    duplicate_count = int(features.duplicated("slug").sum())
    features = features.drop_duplicates("slug", keep="first").reset_index(drop=True)
    features["decision_ts"] = pd.to_datetime(features["first_quote_ts"], utc=True) + pd.Timedelta(seconds=float(config["decision_cutoff_seconds"]))
    bounds = features.groupby("run_name")["decision_ts"].agg(["min", "max"]).sort_values(["min"])
    # A run crossing a preceding run's observation interval is not a separate test block.
    retained = []
    previous_end = None
    rejected_overlap = []
    for name, row in bounds.iterrows():
        if previous_end is not None and row["min"] <= previous_end:
            rejected_overlap.append(str(name))
            continue
        retained.append(str(name)); previous_end = row["max"]
    features = features[features["run_name"].astype(str).isin(retained)].copy()
    keys = set(features["market_id"].astype(str))
    quote_index = {}
    conflicting = sorted(conflicts)
    quote_columns = ["ts_utc", "buy_up_cents", "buy_down_cents", "sell_up_cents", "sell_down_cents",
                     "buy_up_size", "buy_down_size", "sell_up_size", "sell_down_size"]
    for market_id, group in quotes[quotes["market_id"].astype(str).isin(keys)].groupby("market_id", sort=False):
        # market_id includes the retained source run as well as the market slug.
        frame = group[quote_columns].sort_values("ts_utc").copy()
        if frame.duplicated("ts_utc").any():
            conflicting.append(str(market_id))
            continue
        quote_index[str(market_id)] = frame.reset_index(drop=True)
    return features, retained, {"complete_archive_runs": len(complete), "feature_rows_before_quality": before,
        "quality_admission": admission,
        "retained_feature_rows": len(features), "duplicate_market_rows_removed": duplicate_count,
        "qualified_terminal_labels": int(features["outcome_up"].notna().sum()),
        "strict_at_or_after_close_terminal_labels": int((features["outcome_up"].notna() & features["terminal_proxy_gap_seconds"].ge(0)).sum()),
        "bounded_preclose_proxy_labels": int((features["outcome_up"].notna() & features["terminal_proxy_gap_seconds"].lt(0)).sum()),
        "unknown_terminal_labels": int(features["outcome_up"].isna().sum()),
        "conflicting_quote_markets_excluded": conflicting,
        "overlapping_runs_excluded": rejected_overlap, "quality_basis": "decision_prefix_only_no_imputation",
        "label_basis": "declared_bounded_close_price_proxy_not_authenticated_resolution",
        "terminal_proxy_max_gap_seconds": float(config["terminal_proxy_max_gap_seconds"])}, quote_index


def replay_policy(candidates: pd.DataFrame, run_names: list[str], quotes: dict[str, pd.DataFrame], *,
                  latency_seconds: float, depth_haircut: float, thresholds: tuple[float, ...],
                  cash_release_delay_after_close_seconds: float = 900.0) -> dict[str, Any]:
    """Debit a selected entry before inspecting its subsequent execution path."""
    cash = float(signal.INITIAL_CASH)
    chain: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    pending = None
    locked_shares = 0.0
    available_ts = None
    failure = None
    if candidates.empty:
        return {"fills": 0, "ending_cash": cash, "first_ten_fills": 0,
                "first_ten_ending_cash": cash, "first_ten_passed": False,
                "failure_reason": "no_candidates", "ending_cash_available_ts": None, "chain": [], "attempts": []}
    ordered = candidates[candidates["run_name"].astype(str).isin(run_names)].sort_values(["entry_ts", "slug"])
    for row in ordered.itertuples(index=False):
        intended = pd.Timestamp(row.entry_ts) + pd.Timedelta(seconds=float(latency_seconds))
        close = pd.Timestamp(row.market_close_ts)
        if intended >= close:
            attempts.append({"market_id": str(row.market_id), "status": "entry_after_market_close"}); continue
        if pending is not None:
            if intended <= pending[0]:
                attempts.append({"market_id": str(row.market_id), "status": "prior_cash_not_released"}); continue
            cash += pending[1]
            available_ts = pending[0]
            pending = None
        book = quotes.get(str(row.market_id))
        if book is None or book.empty:
            attempts.append({"market_id": str(row.market_id), "status": "no_retained_source_book"}); continue
        later = book[(book["ts_utc"] >= intended) & (book["ts_utc"] < close)]
        if later.empty:
            attempts.append({"market_id": str(row.market_id), "status": "no_delayed_entry_quote"}); continue
        entry_quote = later.iloc[0]
        side = str(row.side)
        price = float(entry_quote[f"buy_{side}_cents"]) / 100.0 + float(row.slippage)
        size = float(entry_quote[f"buy_{side}_size"])
        if not np.isfinite([price, size]).all() or not 0 < price < 1:
            attempts.append({"market_id": str(row.market_id), "status": "invalid_delayed_entry_quote"}); continue
        fraction, ratio = execution.depth_proxy_fraction(row, cash, thresholds)
        fee_per_share = signal.TAKER_FEE_RATE * price * (1 - price)
        gross_target = max(signal.MINIMUM_ORDER_USD, cash * fraction)
        shares = max(signal.MINIMUM_ORDER_SHARES, gross_target / price)
        if shares * (price + fee_per_share) > cash:
            shares = cash / (price + fee_per_share)
        minimum_shares = max(signal.MINIMUM_ORDER_SHARES, signal.MINIMUM_ORDER_USD / price)
        if shares + 1e-9 < minimum_shares:
            failure = "cash_fraction_cannot_fit_minimum_order"; break
        if size * float(depth_haircut) < shares:
            attempts.append({"market_id": str(row.market_id), "status": "delayed_entry_depth_insufficient"}); continue
        cost = shares * price
        fee = shares * fee_per_share
        reserved = cost + fee
        before = cash
        cash -= reserved  # Every entered position remains on the ledger, including failures.
        entry_ts = pd.Timestamp(entry_quote["ts_utc"])
        available_ts = entry_ts
        decision_time = getattr(row, "decision_ts", pd.NaT)
        decision_probability = getattr(row, "q_side", np.nan)
        label_time_reported = getattr(row, "label_observed_ts", pd.NaT)
        proxy_time = getattr(row, "terminal_proxy_observed_ts", pd.NaT)
        proxy_gap = getattr(row, "terminal_proxy_gap_seconds", np.nan)
        event = {"market_id": str(row.market_id), "run_name": str(row.run_name), "slug": str(row.slug),
                 "entry_ts": entry_ts.isoformat(), "cash_before_entry": before, "cash_after_entry": cash,
                 "shares": shares, "entry_fill_price": price, "entry_cost": cost, "entry_fee": fee,
                 "intended_arrival_ts": intended.isoformat(), "entry_quote_gap_seconds": (entry_ts - intended).total_seconds(),
                 "effective_entry_latency_seconds": (entry_ts - pd.Timestamp(getattr(row, "decision_ts", row.entry_ts))).total_seconds(),
                 "fraction": fraction, "depth_capacity_ratio": ratio, "status": "open",
                 "side": side,
                 "q_side": float(decision_probability) if pd.notna(decision_probability) else None,
                 "decision_ts": pd.Timestamp(decision_time).isoformat() if pd.notna(decision_time) else None,
                 "entry_quote_evidence": {
                     "observed_ts": entry_ts.isoformat(),
                     "ask_price": float(entry_quote[f"buy_{side}_cents"]) / 100.0,
                     "available_shares": size, "depth_haircut": float(depth_haircut),
                     "effective_available_shares": size * float(depth_haircut),
                     "slippage": float(row.slippage),
                 },
                 "terminal_proxy_evidence": {
                     "market_close_ts": close.isoformat(),
                     "label_available_ts": pd.Timestamp(label_time_reported).isoformat() if pd.notna(label_time_reported) else None,
                     "observed_ts": pd.Timestamp(proxy_time).isoformat() if pd.notna(proxy_time) else None,
                     "gap_seconds": float(proxy_gap) if pd.notna(proxy_gap) else None,
                 }}
        chain.append(event)
        next_attempt = entry_ts
        exit_ts = None
        payout = None
        subsequent = book[(book["ts_utc"] > entry_ts) & (book["ts_utc"] < close)]
        for _, trigger in subsequent.iterrows():
            trigger_ts = pd.Timestamp(trigger["ts_utc"])
            if trigger_ts < next_attempt:
                continue
            trigger_price = float(trigger[f"sell_{side}_cents"]) / 100.0 - float(row.slippage)
            trigger_size = float(trigger[f"sell_{side}_size"])
            if not np.isfinite([trigger_price, trigger_size]).all() or not 0 < trigger_price < 1:
                continue
            trigger_net = shares * (trigger_price - signal.TAKER_FEE_RATE * trigger_price * (1 - trigger_price))
            if trigger_net < reserved or trigger_size * depth_haircut < shares:
                continue
            arrival = trigger_ts + pd.Timedelta(seconds=float(latency_seconds))
            arrivals = subsequent[subsequent["ts_utc"] >= arrival]
            if arrivals.empty:
                break
            observed = arrivals.iloc[0]
            next_attempt = pd.Timestamp(observed["ts_utc"]) + pd.Timedelta(microseconds=1)
            sell_price = float(observed[f"sell_{side}_cents"]) / 100.0 - float(row.slippage)
            sell_size = float(observed[f"sell_{side}_size"])
            if not np.isfinite([sell_price, sell_size]).all() or not 0 < sell_price < 1:
                continue
            net = shares * (sell_price - signal.TAKER_FEE_RATE * sell_price * (1 - sell_price))
            # A marketable limit order cancels when the arrival book misses its limit.
            if net >= reserved and sell_size * depth_haircut >= shares:
                exit_ts, payout = pd.Timestamp(observed["ts_utc"]), float(net)
                event.update(status="quote_exit", exit_ts=exit_ts.isoformat(), exit_fill_price=sell_price,
                             exit_quote_evidence={
                                 "trigger_observed_ts": trigger_ts.isoformat(),
                                 "trigger_bid_price": float(trigger[f"sell_{side}_cents"]) / 100.0,
                                 "trigger_available_shares": trigger_size,
                                 "trigger_effective_available_shares": trigger_size * float(depth_haircut),
                                 "trigger_net_proceeds": trigger_net,
                                 "intended_arrival_ts": arrival.isoformat(),
                                 "arrival_observed_ts": exit_ts.isoformat(),
                                 "arrival_bid_price": float(observed[f"sell_{side}_cents"]) / 100.0,
                                 "arrival_available_shares": sell_size,
                                 "arrival_effective_available_shares": sell_size * float(depth_haircut),
                                 "arrival_net_proceeds": float(net),
                                 "minimum_net_proceeds": reserved,
                                 "latency_seconds": float(latency_seconds),
                                 "depth_haircut": float(depth_haircut),
                                 "slippage": float(row.slippage),
                             })
                break
        if payout is None:
            outcome = getattr(row, "outcome_up", np.nan)
            label_time = getattr(row, "label_observed_ts", pd.NaT)
            if pd.notna(outcome) and pd.notna(label_time) and pd.Timestamp(label_time) >= close:
                won = float(outcome) == (1.0 if side == "up" else 0.0)
                payout = shares if won else 0.0
                exit_ts = pd.Timestamp(label_time)
                event.update(status="terminal_price_proxy", terminal_proxy_won=won)
            else:
                locked_shares = shares
                event.update(status="unresolved_position", payout=None)
                failure = "entered_position_terminal_evidence_unknown"
                break
        release = max(close + pd.Timedelta(seconds=float(cash_release_delay_after_close_seconds)), exit_ts)
        pending = (release, payout)
        event.update(payout=payout, cash_release_ts=release.isoformat())
        if len(chain) >= execution.REQUIRED_FILLS:
            break
    if pending is not None:
        cash += pending[1]
        available_ts = pending[0]
    passed = len(chain) == execution.REQUIRED_FILLS and locked_shares == 0 and cash >= signal.INITIAL_CASH
    return {"fills": len(chain), "ending_cash": cash, "first_ten_fills": len(chain),
            "first_ten_ending_cash": cash, "first_ten_passed": bool(passed),
            "failure_reason": failure or (None if passed else "sequence_or_net_growth_gate_not_met"),
            "locked_shares": locked_shares, "ending_cash_available_ts": available_ts.isoformat() if available_ts is not None else None,
            "chain": chain, "attempts": attempts}


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    config = validate_config(config)
    observed = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    if observed != config["source_commit"]:
        raise ValueError("source_commit_mismatch")
    features, runs, coverage, quotes = prepare_archive(source_root, config)
    frames: dict[float, list[pd.DataFrame]] = {float(s): [] for s in config["slippages"]}
    fit_records = []
    warmup = int(config["minimum_training_runs"])
    with isolated_signal_contract(config["features"], float(config["decision_cutoff_seconds"]) + float(config["entry_delay_seconds"])):
        for index, name in enumerate(runs):
            if index < warmup:
                continue
            current = features[features["run_name"].astype(str).eq(name)]
            train = causal_training_rows(features, runs[:index], current)
            record = {"scored_run": name, "training_rows": len(train), "scored_rows": len(current),
                "decision_min_ts": current["decision_ts"].min().isoformat(),
                "training_label_max_ts": train["label_observed_ts"].max().isoformat() if not train.empty else None}
            if len(train) < 100 or train["outcome_up"].nunique() < 2:
                record["status"] = "insufficient_causal_training_data"
                fit_records.append(record); continue
            model = ExtraTreesClassifier(**config["model_params"])
            model.fit(train[config["features"]], train["outcome_up"].astype(int))
            for slippage in frames:
                frame = cost_checked_candidate_frame(model, pd.Series(dtype=float), current, slippage)
                if not frame.empty:
                    frame = frame[frame["q_side"].ge(float(config["confidence_floor"])) & frame["price"].ge(float(config["favorite_price_floor"]))].copy()
                    frames[slippage].append(frame)
            record["status"] = "fit_with_prior_available_labels"
            fit_records.append(record)
    candidates = {s: pd.concat(items, ignore_index=True) if items else pd.DataFrame() for s, items in frames.items()}
    test_runs = runs[warmup:]
    blocks = disjoint_blocks(test_runs, int(config["evaluation_block_runs"]))
    rows = []
    fitted_scored_runs = sum(record["status"] == "fit_with_prior_available_labels" for record in fit_records)
    status, strategy_evaluated = evaluation_status(len(features), fitted_scored_runs)
    for slippage, frame in (candidates.items() if strategy_evaluated else []):
        for latency in config["latencies_seconds"]:
            for haircut in config["depth_haircuts"]:
                for index, block in enumerate([test_runs] + blocks):
                    replay = replay_policy(frame, block, quotes, latency_seconds=float(latency),
                        depth_haircut=float(haircut), thresholds=tuple(config["depth_thresholds"]),
                        cash_release_delay_after_close_seconds=float(config["cash_release_delay_after_close_seconds"]))
                    rows.append({"scope": "pooled_single_ledger" if index == 0 else "disjoint_diagnostic_block",
                        "block_index": index, "runs": block, "slippage": slippage, "latency_seconds": latency,
                        "depth_haircut": haircut, **replay})
    report = {"format": "offline-policy-audit-v1", "protocol": config,
        "selection_edge_basis": "q_side_minus_fill_price_including_slippage_minus_taker_fee_per_share",
        "cost_gate_version": "fill_price_net_edge_v1",
        "evaluation_status": status, "strategy_evaluated": strategy_evaluated,
        "fitted_scored_runs": fitted_scored_runs,
        "model_scored_rows": sum(record["scored_rows"] for record in fit_records if record["status"] == "fit_with_prior_available_labels"),
        "candidate_rows_by_slippage": {str(s): len(frame) for s, frame in candidates.items()},
        "protocol_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "source_commit": observed, "coverage": coverage, "run_order": runs, "fit_records": fit_records,
        "evaluation_blocks": blocks, "results": rows,
        "evaluation_class": "retrospective_correction_on_previously_examined_archive_not_untouched_holdout",
        "cash_contract": {"initial_cash": signal.INITIAL_CASH, "required_consecutive_fills": execution.REQUIRED_FILLS,
            "same_ledger_within_path": True, "diagnostic_blocks_not_concatenated": True, "prior_release_required": True,
            "execution_model": "debit_first_delayed_entry_and_delayed_limit_exit_quote_simulation",
            "cash_release_model": "assumed_market_close_plus_declared_delay_or_later_observed_exit",
            "terminal_proxy_clock": "declared_bounded_close_proxy_available_no_earlier_than_close"},
        "actual_fills": 0, "verified_canary_fills": 0, "live_orders_enabled": False, "new_data_collected": False}
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, json.loads(args.config.read_text()), args.output)
    print("offline_policy_audit_complete")


if __name__ == "__main__":
    main()
