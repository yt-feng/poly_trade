"""Private chronological audit of a book-price residual forecast.

Feature admission reads decision prefixes only. Future quote targets remain
separate metadata, and absent targets never remove evaluation opportunities.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_entry_limit_audit as integrity
import offline_paired_cost_audit as storage
import timed_quote_execution as execution


BOOK_NUMERIC = [f"{action}_{side}_{suffix}" for action in ("buy", "sell")
                for side in ("up", "down") for suffix in ("cents", "size")]
FEATURES = ["mid_up_prob_open", "mid_up_prob_2m", "mid_up_prob_change_2m",
    "buy_up_price_2m", "buy_down_price_2m", "buy_up_size_2m", "buy_down_size_2m",
    "sell_up_size_2m", "sell_down_size_2m", "size_imbalance_updown_2m",
    "sell_size_imbalance_updown_2m", "spread_up_median_first2m",
    "spread_down_median_first2m", "overround_median_first2m", "quote_count_first2m"]
ARMS = ("ridge_forecast", "bid_persistence")
FILE_GLOB = "*btc-updown-5m_quotes.csv"


def disjoint_blocks(runs: list[str], size: int) -> list[list[str]]:
    return [runs[i:i + size] for i in range(0, len(runs), size)]


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("evaluation_mode") != "book_bid_residual":
        raise ValueError("bid_residual_mode_required")
    if config.get("features") != FEATURES:
        raise ValueError("book_feature_contract_mismatch")
    for field in ("source_commit", "expected_code_commit"):
        if not re.fullmatch(r"[a-f0-9]{40}", str(config.get(field, ""))):
            raise ValueError("invalid_commit_binding")
    runs = config.get("source_runs")
    if (not isinstance(runs, list) or not runs or len(set(runs)) != len(runs)
            or any(not isinstance(r, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", r) for r in runs)
            or config.get("run_order") != runs):
        raise ValueError("invalid_frozen_source_runs")
    for field in ("expected_files_per_run", "minimum_prefix_quotes", "minimum_training_runs",
                  "minimum_training_labels", "evaluation_block_runs", "max_source_rows",
                  "max_feature_rows", "max_scored_runs", "max_replay_cells"):
        if type(config.get(field)) is not int or config[field] < 1:
            raise ValueError("invalid_integer_protocol_or_budget")
    for field in ("opening_tolerance_seconds", "maximum_snapshot_age_seconds", "decision_cutoff_seconds",
                  "entry_delay_seconds", "hold_seconds", "latency_seconds", "max_quote_delay_seconds",
                  "edge_floor", "fee_rate", "slippage", "depth_haircut", "minimum_order_usd",
                  "minimum_order_shares", "cash_release_delay_after_close_seconds", "ridge_alpha"):
        value = config.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid_numeric_protocol")
    if config["decision_cutoff_seconds"] != 120 or config["hold_seconds"] <= 0:
        raise ValueError("unsupported_decision_or_hold_clock")
    if config["ridge_alpha"] != 1 or config.get("ridge_solver") != "svd":
        raise ValueError("fixed_ridge_contract_required")
    if (config.get("initial_cash") != 10 or config.get("required_fills") != 10
            or config["minimum_order_usd"] != 5 or config["minimum_order_shares"] != 5):
        raise ValueError("fixed_cash_and_fill_contract_required")
    if not (0 < config["depth_haircut"] <= 1 and 0 <= config["slippage"] < 1
            and 0 <= config["fee_rate"] < 1 and config["edge_floor"] < 1
            and config["minimum_order_usd"] > 0 and config["minimum_order_shares"] > 0):
        raise ValueError("invalid_execution_contract")
    thresholds = config.get("depth_thresholds")
    if (not isinstance(thresholds, list) or len(thresholds) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in thresholds)
            or not 0 < thresholds[0] < thresholds[1] < thresholds[2]):
        raise ValueError("invalid_depth_contract")
    tests = runs[config["minimum_training_runs"]:]
    cells = 2 * (1 + len(disjoint_blocks(tests, config["evaluation_block_runs"])))
    if len(tests) > config["max_scored_runs"] or cells > config["max_replay_cells"]:
        raise ValueError("computation_budget_exceeded")
    return config


def normalize_book_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """Retain conflicts as unusable timed observations instead of dropping markets."""
    if not set(["slug", "ts_iso", "source_file", *BOOK_NUMERIC]).issubset(raw.columns):
        raise ValueError("incomplete_book_schema")
    frame = raw[["slug", "ts_iso", "source_file", *BOOK_NUMERIC]].copy()
    frame["ts_utc"] = pd.to_datetime(frame["ts_iso"], utc=True, errors="raise")
    if frame["ts_utc"].isna().any():
        raise ValueError("unplaceable_book_clock")
    if not frame["slug"].astype(str).str.fullmatch(r"btc-updown-5m-[0-9]{10}").all():
        raise ValueError("invalid_market_slug")
    for column in BOOK_NUMERIC:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    counts = frame.groupby(["slug", "ts_utc"])[BOOK_NUMERIC].nunique(dropna=False)
    conflicts = set(counts.index[counts.gt(1).any(axis=1)])
    frame = frame.sort_values(["slug", "ts_utc", "source_file"], kind="mergesort").drop_duplicates(["slug", "ts_utc"], keep="last")
    frame["quote_conflict"] = [(slug, stamp) in conflicts for slug, stamp in zip(frame["slug"], frame["ts_utc"])]
    return frame.reset_index(drop=True)


def _valid_book(row: pd.Series) -> bool:
    values = pd.to_numeric(row[BOOK_NUMERIC], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all():
        return False
    return all(0 < float(row[f"sell_{side}_cents"]) <= float(row[f"buy_{side}_cents"]) < 100
               and float(row[f"buy_{side}_size"]) >= 0 and float(row[f"sell_{side}_size"]) >= 0
               for side in ("up", "down"))


def _ratio(a: float, b: float) -> float:
    return (a - b) / (a + b) if a + b > 0 else float("nan")


def prefix_book_features(prefix: pd.DataFrame, *, run_name: str, slug: str,
                         decision_ts: pd.Timestamp, config: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Only the supplied prefix may influence admission or features."""
    if prefix.empty or prefix["ts_utc"].gt(decision_ts).any():
        raise ValueError("feature_input_must_be_nonempty_decision_prefix")
    prefix = prefix.sort_values("ts_utc")
    first = prefix.iloc[0]
    first_ts = pd.Timestamp(first["ts_utc"])
    start = pd.to_datetime(int(slug.rsplit("-", 1)[-1]), unit="s", utc=True)
    close = start + pd.Timedelta(seconds=300)
    if not start <= first_ts <= start + pd.Timedelta(seconds=config["opening_tolerance_seconds"]):
        return None, "opening_clock_not_covered"
    if decision_ts != first_ts + pd.Timedelta(seconds=config["decision_cutoff_seconds"]) or decision_ts >= close:
        return None, "invalid_decision_clock"
    if len(prefix) < config["minimum_prefix_quotes"]:
        return None, "too_few_prefix_quotes"
    if prefix["quote_conflict"].any():
        return None, "conflicting_prefix_quotes"
    numeric = prefix[BOOK_NUMERIC].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        return None, "incomplete_or_invalid_prefix_book"
    for side in ("up", "down"):
        bid = prefix[f"sell_{side}_cents"].to_numpy(dtype=float)
        ask = prefix[f"buy_{side}_cents"].to_numpy(dtype=float)
        if not ((bid > 0) & (bid <= ask) & (ask < 100)
                & (prefix[f"buy_{side}_size"].to_numpy(dtype=float) >= 0)
                & (prefix[f"sell_{side}_size"].to_numpy(dtype=float) >= 0)).all():
            return None, "incomplete_or_invalid_prefix_book"
    snapshots = []
    ages = []
    for offset in (60, 120):
        cutoff = first_ts + pd.Timedelta(seconds=offset)
        eligible = prefix[prefix["ts_utc"] <= cutoff]
        if eligible.empty:
            return None, "missing_prefix_snapshot"
        row = eligible.iloc[-1]
        age = (cutoff - row["ts_utc"]).total_seconds()
        if age > config["maximum_snapshot_age_seconds"] or not _valid_book(row):
            return None, "invalid_or_stale_prefix_snapshot"
        snapshots.append(row); ages.append(age)
    if not _valid_book(first):
        return None, "invalid_opening_book"
    row = snapshots[-1]
    up_mid = (prefix["buy_up_cents"] + prefix["sell_up_cents"]) / 200
    down_mid = (prefix["buy_down_cents"] + prefix["sell_down_cents"]) / 200
    opening_mid = float((first["buy_up_cents"] + first["sell_up_cents"]) / 200)
    current_up = float((row["buy_up_cents"] + row["sell_up_cents"]) / 200)
    current_down = float((row["buy_down_cents"] + row["sell_down_cents"]) / 200)
    record = {"market_id": run_name + "::" + slug, "run_name": run_name, "slug": slug,
        "first_quote_ts": first_ts, "decision_ts": decision_ts, "market_close_ts": close,
        "snapshot_age_seconds_1m": ages[0], "snapshot_age_seconds_2m": ages[1],
        "mid_up_prob_open": opening_mid, "mid_up_prob_2m": current_up,
        "mid_up_prob_change_2m": current_up - opening_mid,
        "buy_up_price_2m": float(row["buy_up_cents"]) / 100,
        "buy_down_price_2m": float(row["buy_down_cents"]) / 100,
        "buy_up_size_2m": float(row["buy_up_size"]), "buy_down_size_2m": float(row["buy_down_size"]),
        "sell_up_size_2m": float(row["sell_up_size"]), "sell_down_size_2m": float(row["sell_down_size"]),
        "size_imbalance_updown_2m": _ratio(float(row["buy_up_size"]), float(row["buy_down_size"])),
        "sell_size_imbalance_updown_2m": _ratio(float(row["sell_up_size"]), float(row["sell_down_size"])),
        "spread_up_median_first2m": float(((prefix["buy_up_cents"] - prefix["sell_up_cents"]) / 100).median()),
        "spread_down_median_first2m": float(((prefix["buy_down_cents"] - prefix["sell_down_cents"]) / 100).median()),
        "overround_median_first2m": float((up_mid + down_mid - 1).median()),
        "quote_count_first2m": len(prefix)}
    if not np.isfinite([record[f] for f in FEATURES]).all():
        return None, "nonfinite_registered_book_feature"
    side = "up" if current_up >= current_down else "down"
    record.update(side=side, decision_bid_price=float(row[f"sell_{side}_cents"]) / 100,
                  decision_ask_price=float(row[f"buy_{side}_cents"]) / 100,
                  decision_sell_size=float(row[f"sell_{side}_size"]))
    return record, "admitted"


def quote_target(row: dict[str, Any] | pd.Series, book: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    target = {"market_id": row["market_id"], "future_bid_price": np.nan, "bid_residual_target": np.nan,
              "label_available_ts": pd.NaT, "target_entry_quote_ts": pd.NaT,
              "target_exit_arrival_ts": pd.NaT, "target_status": "unknown"}
    entry_at = pd.Timestamp(row["decision_ts"]) + pd.Timedelta(seconds=config["entry_delay_seconds"] + config["latency_seconds"])
    entry, reason = execution.select_book_quote(book, entry_at, row["market_close_ts"], side=row["side"],
        kind="entry", max_quote_delay_seconds=config["max_quote_delay_seconds"])
    if entry is None:
        target["target_status"] = "entry_" + reason
        return target
    target["target_entry_quote_ts"] = pd.Timestamp(entry["ts_utc"])
    exit_at = target["target_entry_quote_ts"] + pd.Timedelta(seconds=config["hold_seconds"] + config["latency_seconds"])
    target["target_exit_arrival_ts"] = exit_at
    quote, reason = execution.select_book_quote(book, exit_at, row["market_close_ts"], side=row["side"],
        kind="exit", max_quote_delay_seconds=config["max_quote_delay_seconds"])
    if quote is None:
        target["target_status"] = "exit_" + reason
        return target
    future = float(quote[f"sell_{row['side']}_cents"]) / 100
    target.update(future_bid_price=future, bid_residual_target=future - row["decision_bid_price"],
                  label_available_ts=pd.Timestamp(quote["ts_utc"]), target_status="known_future_bid")
    return target


def prepare_archive(source_root: Path, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, Any]]:
    records = []
    quote_index = {}
    owners: dict[str, str] = {}
    admission = Counter()
    source_rows = duplicate_markets = files_read = 0
    run_coverage = []
    previous_decision_max = None
    for run_name in config["source_runs"]:
        directory = source_root / run_name
        paths = sorted(directory.glob(FILE_GLOB))
        if not directory.resolve().is_relative_to(source_root.resolve()) or len(paths) != config["expected_files_per_run"]:
            raise ValueError("frozen_source_run_file_contract_mismatch")
        pieces = []
        for path in paths:
            if not path.resolve().is_relative_to(source_root.resolve()):
                raise ValueError("source_file_escapes_archive")
            raw = pd.read_csv(path, usecols=lambda c: c.strip() in {"ts_iso", "slug", *BOOK_NUMERIC})
            raw.columns = [str(c).strip() for c in raw.columns]
            raw["source_file"] = path.name
            pieces.append(raw)
            source_rows += len(raw); files_read += 1
            if source_rows > config["max_source_rows"]:
                raise ValueError("source_row_budget_exceeded")
        quotes = normalize_book_rows(pd.concat(pieces, ignore_index=True))
        before = len(records)
        reasons = Counter()
        run_decisions = []
        for slug, group in quotes.groupby("slug", sort=False):
            if slug in owners:
                duplicate_markets += 1
                continue
            owners[str(slug)] = run_name
            group = group.sort_values("ts_utc").reset_index(drop=True)
            decision = group["ts_utc"].iloc[0] + pd.Timedelta(seconds=config["decision_cutoff_seconds"])
            prefix = group[group["ts_utc"] <= decision]
            feature, reason = prefix_book_features(prefix, run_name=run_name, slug=str(slug), decision_ts=decision, config=config)
            admission[reason] += 1; reasons[reason] += 1
            if feature is None:
                continue
            if len(records) >= config["max_feature_rows"]:
                raise ValueError("feature_row_budget_exceeded")
            book = group[["ts_utc", *BOOK_NUMERIC, "quote_conflict"]].copy()
            quote_index[feature["market_id"]] = book
            # Targets never participate in admission, identity choice or ordering.
            feature.update(quote_target(feature, book, config))
            records.append(feature); run_decisions.append(decision)
        if run_decisions:
            if previous_decision_max is not None and min(run_decisions) <= previous_decision_max:
                raise ValueError("frozen_run_decision_intervals_overlap")
            previous_decision_max = max(run_decisions)
        run_coverage.append({"run_name": run_name, "raw_rows": len(quotes), "admitted_markets": len(records) - before,
                             "prefix_admission": dict(reasons)})
    columns = ["market_id", "run_name", "slug", "first_quote_ts", "decision_ts", "market_close_ts",
               *FEATURES, "side", "decision_bid_price", "decision_ask_price", "decision_sell_size",
               "future_bid_price", "bid_residual_target", "label_available_ts", "target_entry_quote_ts",
               "target_exit_arrival_ts", "target_status"]
    frame = pd.DataFrame(records) if records else pd.DataFrame(columns=columns)
    for column in ("first_quote_ts", "decision_ts", "market_close_ts", "label_available_ts",
                   "target_entry_quote_ts", "target_exit_arrival_ts"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    if not frame.empty:
        frame = frame.sort_values(["decision_ts", "run_name", "slug"], kind="mergesort").reset_index(drop=True)
    return frame, quote_index, {"frozen_source_runs": len(config["source_runs"]), "files_read": files_read,
        "source_rows": source_rows, "admitted_markets": len(frame), "duplicate_market_sources_ignored": duplicate_markets,
        "prefix_admission": dict(admission), "per_run": run_coverage,
        "target_status_counts": dict(Counter(frame["target_status"])),
        "raw_numeric_fields_read": BOOK_NUMERIC, "admission_basis": "observed_book_prefix_only",
        "target_basis": "future_same_side_bid_after_actual_entry_quote_plus_fixed_hold_and_exit_latency",
        "clock_basis": "archived_snapshot_wall_clock_not_certified_per_request_receipts"}


def causal_training_rows(frame: pd.DataFrame, prior_runs: list[str], current: pd.DataFrame) -> pd.DataFrame:
    if current.empty:
        return frame.iloc[:0].copy()
    cutoff = current["decision_ts"].min()
    known = frame["label_available_ts"].notna() & frame["bid_residual_target"].notna()
    train = frame[known & frame["run_name"].isin(prior_runs) & frame["label_available_ts"].lt(cutoff)].copy()
    if set(train["slug"]) & set(current["slug"]):
        raise ValueError("training_evaluation_market_overlap")
    return train


def fit_predict_once(train: pd.DataFrame, current: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    model = make_pipeline(StandardScaler(), Ridge(alpha=float(config["ridge_alpha"]), solver=config["ridge_solver"]))
    model.fit(train[FEATURES], train["bid_residual_target"])
    residual = np.asarray(model.predict(current[FEATURES]), dtype=float)
    if not np.isfinite(residual).all():
        raise ValueError("nonfinite_residual_prediction")
    out = current.copy()
    out["predicted_bid_residual_raw"] = residual
    out["predicted_exit_bid_unclipped"] = out["decision_bid_price"] + residual
    out["predicted_exit_bid_price"] = out["predicted_exit_bid_unclipped"].clip(0, 1)
    scaler, ridge = model.steps[0][1], model.steps[1][1]
    return out, {"scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
                 "ridge_coef": ridge.coef_.tolist(), "ridge_intercept": float(ridge.intercept_)}


def prediction_metrics(scored: pd.DataFrame) -> dict[str, Any]:
    known = scored[scored["future_bid_price"].notna()].copy()
    def metric(part: pd.DataFrame) -> dict[str, Any]:
        if part.empty:
            return {"known_targets": 0, "model_squared_error_sum": None, "baseline_squared_error_sum": None,
                    "model_mse": None, "baseline_mse": None, "model_beats_persistence": None}
        actual = part["future_bid_price"].to_numpy(dtype=float)
        model_errors = (part["predicted_exit_bid_price"].to_numpy(dtype=float) - actual) ** 2
        baseline_errors = (part["decision_bid_price"].to_numpy(dtype=float) - actual) ** 2
        return {"known_targets": len(part), "model_squared_error_sum": float(model_errors.sum()),
                "baseline_squared_error_sum": float(baseline_errors.sum()), "model_mse": float(model_errors.mean()),
                "baseline_mse": float(baseline_errors.mean()), "model_beats_persistence": bool(model_errors.sum() < baseline_errors.sum())}
    return {"aggregate_known_targets": metric(known), "scored_rows": len(scored),
            "unknown_targets_retained": int(scored["future_bid_price"].isna().sum()),
            "per_run": [{"run_name": str(run), **metric(group)} for run, group in known.groupby("run_name", sort=False)],
            "basis": "paired_prediction_errors_on_known_future_bid_targets_only_not_profit_or_independent_trials"}


def decision_cost_candidates(scored: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Determine intent from decision-time quotes before consulting later books."""
    out = scored.copy()
    entry = pd.to_numeric(out["decision_ask_price"], errors="coerce") + config["slippage"]
    exit_price = pd.to_numeric(out["predicted_exit_bid_price"], errors="coerce") - config["slippage"]
    valid = entry.between(0, 1, inclusive="neither") & exit_price.between(0, 1, inclusive="neither")
    fee = config["fee_rate"]
    margin = exit_price - fee * exit_price * (1 - exit_price) - entry - fee * entry * (1 - entry)
    out["decision_expected_net_margin"] = margin.where(valid, np.nan)
    out["decision_signal_admitted"] = valid & margin.ge(config["edge_floor"])
    return out.loc[out["decision_signal_admitted"]].copy(), out


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    validate_config(config)
    source_commit, code_commit = integrity.verified_commits(source_root, config)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    frame, quotes, coverage = prepare_archive(source_root, config)
    storage._private_csv(output / "features.csv", frame)
    runs = config["run_order"]
    test_runs = runs[config["minimum_training_runs"]:]
    blocks = disjoint_blocks(test_runs, config["evaluation_block_runs"])
    fits = []
    predictions = []
    models = []
    members = []
    for index, name in enumerate(runs):
        if index < config["minimum_training_runs"]:
            continue
        current = frame[frame["run_name"].eq(name)].copy()
        train = causal_training_rows(frame, runs[:index], current)
        cutoff = current["decision_ts"].min() if not current.empty else None
        ids = sorted(train["market_id"].astype(str))
        known_runs = int(train["run_name"].nunique())
        record = {"run_name": name, "training_rows": len(train), "training_known_runs": known_runs, "scored_rows": 0,
                  "evaluation_cohort_rows": len(current), "decision_cutoff_ts": cutoff.isoformat() if cutoff is not None else None,
                  "training_label_max_ts": train["label_available_ts"].max().isoformat() if not train.empty else None,
                  "training_market_ids_sha256": hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest(),
                  "actual_model_fit_calls": 0, "actual_model_predict_calls": 0}
        if current.empty or len(train) < config["minimum_training_labels"] or known_runs < config["minimum_training_runs"]:
            record["status"] = ("empty_evaluation_cohort" if current.empty else
                "insufficient_prior_labeled_runs" if known_runs < config["minimum_training_runs"] else "insufficient_prior_known_quote_targets")
            fits.append(record)
            continue
        scored, model = fit_predict_once(train, current, config)
        scored["fit_run"] = name
        predictions.append(scored)
        models.append({"fit_run": name, "cutoff_ts": cutoff.isoformat(), **model})
        member = train[["market_id", "label_available_ts", "bid_residual_target"]].copy()
        member.insert(0, "fit_run", name); member["decision_cutoff_ts"] = cutoff
        members.append(member)
        record.update(status="fit_with_prior_available_labels", scored_rows=len(scored),
                      actual_model_fit_calls=1, actual_model_predict_calls=1)
        fits.append(record)
    scored = pd.concat(predictions, ignore_index=True) if predictions else frame.iloc[:0].assign(
        predicted_bid_residual_raw=pd.Series(dtype=float), predicted_exit_bid_unclipped=pd.Series(dtype=float),
        predicted_exit_bid_price=pd.Series(dtype=float), fit_run=pd.Series(dtype=str))
    storage._private_csv(output / "scores.csv", scored)
    storage._private_csv(output / "training-members.csv", pd.concat(members, ignore_index=True) if members else pd.DataFrame(
        columns=["fit_run", "market_id", "label_available_ts", "bid_residual_target", "decision_cutoff_ts"]))
    storage._private_json(output / "fitted-models.json", models)
    evaluated = not scored.empty
    arm_reports = {}
    for arm in ARMS:
        opportunities = scored.copy()
        if arm == "bid_persistence":
            opportunities["predicted_exit_bid_price"] = opportunities["decision_bid_price"]
        candidates, decisions = decision_cost_candidates(opportunities, config)
        storage._private_csv(output / f"{arm}-decisions.csv", decisions)
        storage._private_csv(output / f"{arm}-candidates.csv", candidates)
        results = []
        if evaluated:
            for index, scope_runs in enumerate([test_runs] + blocks):
                result = execution.replay_timed_quotes(candidates, scope_runs, quotes,
                    hold_seconds=config["hold_seconds"], entry_delay_seconds=config["entry_delay_seconds"],
                    latency_seconds=config["latency_seconds"], max_quote_delay_seconds=config["max_quote_delay_seconds"],
                    edge_floor=config["edge_floor"], fee_rate=config["fee_rate"], slippage=config["slippage"],
                    depth_haircut=config["depth_haircut"], thresholds=tuple(config["depth_thresholds"]),
                    cash_release_delay_after_close_seconds=config["cash_release_delay_after_close_seconds"],
                    initial_cash=config["initial_cash"], minimum_order_usd=config["minimum_order_usd"],
                    minimum_order_shares=config["minimum_order_shares"], required_fills=config["required_fills"])
                results.append({"scope": "pooled_single_ledger" if index == 0 else "disjoint_diagnostic_block",
                    "block_index": index, "runs": scope_runs, "slippage": config["slippage"],
                    "latency_seconds": config["latency_seconds"], "depth_haircut": config["depth_haircut"], **result})
        arm_reports[arm] = {"prediction_basis": "frozen_ridge_bid_residual" if arm == "ridge_forecast" else "current_bid_persistence",
                            "evaluation_cohort_rows": len(opportunities), "candidate_rows": len(candidates),
                            "decision_cost_floor_rejections": len(opportunities) - len(candidates), "results": results}
    fit_count = sum(r["actual_model_fit_calls"] for r in fits)
    status = "evaluated" if evaluated else "not_evaluated_empty_quality_dataset" if frame.empty else "not_evaluated_insufficient_training_data"
    artifacts = {f.name: {"sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "bytes": f.stat().st_size}
                 for f in sorted(output.iterdir()) if f.is_file()}
    report = {"format": "offline-bid-residual-audit-v1", "protocol": config,
        "protocol_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "source_commit": source_commit, "code_commit": code_commit, "run_order": runs, "test_runs": test_runs,
        "evaluation_blocks": blocks, "coverage": coverage, "evaluation_status": status, "strategy_evaluated": bool(evaluated),
        "actual_model_fit_calls": fit_count, "actual_model_predict_calls": sum(r["actual_model_predict_calls"] for r in fits),
        "fitted_scored_runs": fit_count, "model_scored_rows": len(scored), "fit_records": fits,
        "replayed_cells": sum(len(arm["results"]) for arm in arm_reports.values()),
        "prediction_metrics": prediction_metrics(scored), "arms": arm_reports, "artifacts": artifacts,
        "comparison_contract": {"same_test_cohort_for_model_and_baseline": True,
            "baseline": "current_bid_persistence_plus_uninvested_cash",
            "unknown_future_targets_do_not_remove_evaluation_rows": True,
            "terminal_price_or_resolution_targets_used": False,
            "feature_input": "eight_observed_best_book_price_and_size_fields_only",
            "feature_semantics": "Reconstruct midpoints/spreads from observed bid/ask; do not inherit rounded legacy mid/spread columns. Favorite compares both midpoints with Up ties. Not bitwise-equivalent to prior feature sets.",
            "future_bid_is_quote_evidence_not_authenticated_execution": True},
        "evaluation_class": "retrospective_new_quote_target_family_not_untouched_holdout_or_single_factor_prior_policy_comparison",
        "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False}
    storage._private_json(output / "summary.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, json.loads(args.config.read_text()), args.output)


if __name__ == "__main__":
    main()
