"""Private paired execution audit of an immutable candidate projection.

This module reuses predictions without fitting or scoring a model. Historical
book replay remains separate from authenticated fills and cash receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_policy_audit as base
import offline_paired_cost_audit as paired


ARMS = ("uncapped_control", "decision_cost_capped")
CANDIDATE_FIELDS = (
    "market_id", "run_name", "slug", "side", "slippage", "entry_ts", "decision_ts",
    "market_close_ts", "label_observed_ts", "terminal_proxy_observed_ts",
    "terminal_proxy_gap_seconds", "outcome_up", "q_side", "price", "fill_price",
    "sell_up_size_2m", "sell_down_size_2m",
)
NUMERIC_FIELDS = ("slippage", "q_side", "price", "fill_price", "sell_up_size_2m", "sell_down_size_2m")
NULLABLE_NUMERIC = ("terminal_proxy_gap_seconds", "outcome_up")
TIME_FIELDS = ("entry_ts", "decision_ts", "market_close_ts")
NULLABLE_TIMES = ("label_observed_ts", "terminal_proxy_observed_ts")
QUOTE_FIELDS = ["ts_utc", "buy_up_cents", "buy_down_cents", "sell_up_cents", "sell_down_cents",
                "buy_up_size", "buy_down_size", "sell_up_size", "sell_down_size"]


def candidate_rows_sha256(rows: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    base.validate_config(config)
    if config.get("evaluation_mode") != "paired_entry_limit":
        raise ValueError("entry_limit_mode_required")
    for key in ("source_commit", "expected_code_commit", "parent_code_commit"):
        if not re.fullmatch(r"[a-f0-9]{40}", str(config.get(key, ""))):
            raise ValueError("invalid_commit_binding")
    for key in ("parent_summary_sha256", "parent_candidate_csv_sha256", "frozen_candidate_rows_sha256",
                "control_reference_results_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(config.get(key, ""))):
            raise ValueError("invalid_artifact_binding")
    for key in ("max_candidate_rows", "max_replay_cells"):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError("invalid_computation_budget")
    for key in ("max_quote_files", "max_quote_rows"):
        if key in config and (type(config[key]) is not int or config[key] < 1):
            raise ValueError("invalid_quote_budget")
    floor = config.get("entry_edge_floor")
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor) or not 0 <= floor < 1:
        raise ValueError("invalid_entry_edge_floor")
    for key in ("slippages", "latencies_seconds", "depth_haircuts"):
        values = list(map(float, config[key]))
        if not all(math.isfinite(v) for v in values) or len(set(values)) != len(values):
            raise ValueError("invalid_or_duplicate_stress_cell")
    runs = config.get("run_order")
    if not isinstance(runs, list) or not runs or any(not isinstance(r, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", r) for r in runs) or len(set(runs)) != len(runs):
        raise ValueError("invalid_frozen_run_order")
    test_runs = config.get("test_runs")
    if test_runs != runs[int(config["minimum_training_runs"]):]:
        raise ValueError("frozen_test_partition_mismatch")
    if config.get("evaluation_blocks") != base.disjoint_blocks(test_runs, int(config["evaluation_block_runs"])):
        raise ValueError("frozen_diagnostic_partition_mismatch")
    counts = paired._candidate_counts(config.get("control_reference_candidate_counts", {}))
    if set(counts) != {str(float(v)) for v in config["slippages"]}:
        raise ValueError("reference_slippage_mismatch")
    cells = 2 * len(config["slippages"]) * len(config["latencies_seconds"]) * len(config["depth_haircuts"]) * (1 + len(config["evaluation_blocks"]))
    if cells > config["max_replay_cells"]:
        raise ValueError("declared_replay_budget_exceeded")
    return config


def frozen_candidates(config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, int]]:
    rows = config.get("frozen_candidates")
    if not isinstance(rows, list) or len(rows) > config["max_candidate_rows"]:
        raise ValueError("invalid_frozen_candidate_budget")
    if any(not isinstance(r, dict) or set(r) != set(CANDIDATE_FIELDS) or any(not isinstance(v, str) for v in r.values()) for r in rows):
        raise ValueError("invalid_frozen_candidate_projection")
    if candidate_rows_sha256(rows) != config["frozen_candidate_rows_sha256"]:
        raise ValueError("frozen_candidate_hash_mismatch")
    frame = pd.DataFrame(rows, columns=CANDIDATE_FIELDS)
    for column in NUMERIC_FIELDS:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
        if not np.isfinite(frame[column].to_numpy(dtype=float)).all():
            raise ValueError("nonfinite_execution_input")
    for column in NULLABLE_NUMERIC:
        frame[column] = pd.to_numeric(frame[column].replace("", np.nan), errors="raise")
        if np.isinf(frame[column].to_numpy(dtype=float)).any():
            raise ValueError("infinite_terminal_metadata")
    for column in TIME_FIELDS + NULLABLE_TIMES:
        frame[column] = pd.to_datetime(frame[column].replace("", None), utc=True, errors="raise")
        if column in TIME_FIELDS and frame[column].isna().any():
            raise ValueError("missing_execution_clock")
    if frame.duplicated(["slippage", "market_id"]).any():
        raise ValueError("duplicate_frozen_candidate")
    if not frame["run_name"].isin(config["test_runs"]).all() or not frame["side"].isin(["up", "down"]).all():
        raise ValueError("candidate_scope_mismatch")
    if not frame["market_id"].eq(frame["run_name"] + "::" + frame["slug"]).all() or not frame["slug"].str.fullmatch(r"btc-updown-5m-[0-9]{10}").all():
        raise ValueError("candidate_identity_mismatch")
    if frame.groupby("slug")["run_name"].nunique().gt(1).any():
        raise ValueError("candidate_market_reused_across_runs")
    if not frame["slippage"].isin(list(map(float, config["slippages"]))).all():
        raise ValueError("candidate_slippage_mismatch")
    if not frame["q_side"].between(0, 1).all() or not frame["price"].between(0, 1, inclusive="neither").all() or not frame["fill_price"].between(0, 1, inclusive="neither").all():
        raise ValueError("invalid_execution_price_or_probability")
    if not np.allclose(frame["fill_price"], frame["price"] + frame["slippage"], atol=1e-12, rtol=0):
        raise ValueError("frozen_fill_price_mismatch")
    if frame[["sell_up_size_2m", "sell_down_size_2m"]].lt(0).any().any():
        raise ValueError("invalid_frozen_depth")
    close = pd.to_datetime(frame["slug"].str.rsplit("-", n=1).str[-1].astype("int64"), unit="s", utc=True) + pd.Timedelta(seconds=300)
    if not frame["market_close_ts"].eq(close).all() or not frame["decision_ts"].lt(close).all() or not frame["entry_ts"].eq(frame["decision_ts"] + pd.Timedelta(seconds=float(config["entry_delay_seconds"]))).all():
        raise ValueError("frozen_clock_mismatch")
    if not frame["outcome_up"].dropna().isin([0, 1]).all() or not frame["outcome_up"].notna().eq(frame["label_observed_ts"].notna()).all():
        raise ValueError("inconsistent_terminal_metadata")
    counts = {str(float(v)): int(frame["slippage"].eq(float(v)).sum()) for v in config["slippages"]}
    if counts != paired._candidate_counts(config["control_reference_candidate_counts"]):
        raise ValueError("frozen_candidate_count_mismatch")
    return frame, counts


def read_candidate_quotes(source_root: Path, frame: pd.DataFrame, config: dict[str, Any]) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Read only candidate runs, retaining exact run/slug identity and conflicts."""
    selected = frame[["run_name", "slug"]].drop_duplicates()
    pieces = []
    scanned_files = scanned_rows = retained_rows = 0
    maximum_files = int(config.get("max_quote_files", 5000))
    maximum_rows = int(config.get("max_quote_rows", 10_000_000))
    for run_name, group in selected.groupby("run_name", sort=False):
        run_dir = source_root / str(run_name)
        if not run_dir.resolve().is_relative_to(source_root.resolve()):
            raise ValueError("source_run_escapes_archive")
        wanted = set(group["slug"])
        for path in sorted(run_dir.glob(base.source.FILE_GLOB)):
            if not path.resolve().is_relative_to(source_root.resolve()):
                raise ValueError("source_file_escapes_archive")
            scanned_files += 1
            if scanned_files > maximum_files:
                raise ValueError("quote_file_budget_exceeded")
            for chunk in pd.read_csv(path, chunksize=10000):
                scanned_rows += len(chunk)
                if scanned_rows > maximum_rows:
                    raise ValueError("quote_row_budget_exceeded")
                chunk.columns = [str(c).strip() for c in chunk.columns]
                part = chunk[chunk["slug"].astype(str).isin(wanted)].copy()
                if part.empty:
                    continue
                part["run_name"] = str(run_name)
                part["source_file"] = path.name
                pieces.append(part)
                retained_rows += len(part)
    raw = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=["run_name", "slug", "ts_iso"])
    duplicate = raw[raw.duplicated(["run_name", "slug", "ts_iso"], keep=False)]
    conflicts = set()
    if not duplicate.empty:
        columns = [c for c in base.source.REQUIRED_NUMERIC if c in duplicate]
        different = duplicate.groupby(["run_name", "slug", "ts_iso"])[columns].nunique(dropna=False)
        conflicts = {str(run) + "::" + str(slug) for run, slug, _ in different.index[different.gt(1).any(axis=1)]}
    quotes = base.source.prepare_quotes(raw)
    keys = set(frame["market_id"])
    index = {}
    for key, group in quotes[quotes["market_id"].isin(keys - conflicts)].groupby("market_id", sort=False):
        book = group[QUOTE_FIELDS].sort_values("ts_utc").copy()
        if book.duplicated("ts_utc").any():
            conflicts.add(str(key))
        else:
            index[str(key)] = book.reset_index(drop=True)
    return index, {"scanned_files": scanned_files, "scanned_rows": scanned_rows,
        "retained_raw_rows": retained_rows, "requested_markets": len(keys), "quote_markets": len(index),
        "conflicting_quote_markets_excluded": sorted(conflicts), "missing_quote_markets": sorted(keys - set(index)),
        "quote_selection": "frozen_candidate_run_and_slug_only_no_feature_reselection"}


def replay_arm(frame: pd.DataFrame, quotes: dict[str, pd.DataFrame], config: dict[str, Any], capped: bool) -> list[dict[str, Any]]:
    results = []
    for slippage in config["slippages"]:
        candidates = frame[frame["slippage"].eq(float(slippage))].copy()
        for latency in config["latencies_seconds"]:
            for haircut in config["depth_haircuts"]:
                for index, runs in enumerate([config["test_runs"]] + config["evaluation_blocks"]):
                    result = base.replay_policy(candidates, runs, quotes, latency_seconds=float(latency),
                        depth_haircut=float(haircut), thresholds=tuple(config["depth_thresholds"]),
                        cash_release_delay_after_close_seconds=float(config["cash_release_delay_after_close_seconds"]),
                        entry_edge_floor=float(config["entry_edge_floor"]) if capped else None)
                    results.append({"scope": "pooled_single_ledger" if index == 0 else "disjoint_diagnostic_block",
                        "block_index": index, "runs": runs, "slippage": slippage, "latency_seconds": latency,
                        "depth_haircut": haircut, **result})
    return results


def verified_commits(source_root: Path, config: dict[str, Any]) -> tuple[str, str]:
    source = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    code = subprocess.check_output(["git", "-C", str(CURRENT_DIR), "rev-parse", "HEAD"], text=True).strip()
    if source != config["source_commit"] or code != config["expected_code_commit"]:
        raise ValueError("source_or_code_commit_mismatch")
    clean = subprocess.run(["git", "-C", str(source_root), "diff", "--quiet", "HEAD", "--", "."], capture_output=True)
    if clean.returncode != 0:
        raise ValueError("source_archive_tracked_changes")
    untracked = subprocess.check_output(
        # Include ignored files: the quote glob would read an ignored CSV too.
        ["git", "-C", str(source_root), "ls-files", "--others", "--", "."],
        text=True,
    )
    if untracked.strip():
        raise ValueError("source_archive_untracked_files")
    code_clean = subprocess.run(["git", "-C", str(CURRENT_DIR), "diff", "--quiet", "HEAD", "--", "."], capture_output=True)
    if code_clean.returncode != 0:
        raise ValueError("executed_code_tracked_changes")
    return source, code


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    validate_config(config)
    frame, counts = frozen_candidates(config)
    source_commit, code_commit = verified_commits(source_root, config)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    quotes, coverage = read_candidate_quotes(source_root, frame, config)
    evaluated = bool(not frame.empty and config["test_runs"])
    arms = {name: {"candidate_rows_by_slippage": counts,
                   "results": replay_arm(frame, quotes, config, name == "decision_cost_capped") if evaluated else []}
            for name in ARMS}
    control = paired.compare_control(arms["uncapped_control"]["results"], counts, config)
    cells = sum(len(arm["results"]) for arm in arms.values())
    evaluated = bool(evaluated and cells > 0)
    reproduced = evaluated and control["control_reproduced"]
    status = ("evaluated" if reproduced else "evaluated_control_mismatch") if evaluated else "not_evaluated_empty_frozen_candidates"
    paired._private_json(output / "frozen-candidates.json", config["frozen_candidates"])
    paired._private_csv(output / "frozen-candidates.csv", pd.DataFrame(config["frozen_candidates"], columns=CANDIDATE_FIELDS))
    artifacts = {path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                 for path in sorted(output.iterdir()) if path.is_file()}
    protocol = {key: value for key, value in config.items() if key != "frozen_candidates"}
    report = {"format": "offline-entry-limit-audit-v1", "protocol": protocol,
        "protocol_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "source_commit": source_commit, "code_commit": code_commit, "coverage": coverage,
        "run_order": config["run_order"], "test_runs": config["test_runs"], "evaluation_blocks": config["evaluation_blocks"],
        "evaluation_status": status, "strategy_evaluated": evaluated, "replay_evaluated": evaluated,
        "candidate_rows_reused": len(frame), "replayed_cells": cells,
        "actual_model_fit_calls": 0, "actual_model_predict_calls": 0, "fit_records": [],
        "arms": arms, "control_verification": control, "control_reproduced": bool(reproduced),
        "causal_comparison_valid": bool(reproduced), "artifacts": artifacts,
        "candidate_provenance": {"rows_sha256_verified": config["frozen_candidate_rows_sha256"],
            "parent_candidate_csv_sha256": config["parent_candidate_csv_sha256"],
            "parent_summary_sha256": config["parent_summary_sha256"], "parent_code_commit": config["parent_code_commit"],
            "original_file_hash_basis": "bound_by_private_input_projection_manifest_original_bytes_not_rehashed_by_this_runner",
            "projection_fields": list(CANDIDATE_FIELDS)},
        "paired_contract": {"same_frozen_candidates_and_probabilities": True,
            "only_arm_difference": "decision_probability_cost_limit_at_delayed_entry_arrival",
            "fingerprint_version": paired.FINGERPRINT_VERSION,
            "entry_limit_basis": "continuous_price_cap_no_tick_size_or_actual_order_claim"},
        "evaluation_class": "retrospective_paired_execution_correction_not_untouched_holdout",
        "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False}
    paired._private_json(output / "summary.json", report)
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
