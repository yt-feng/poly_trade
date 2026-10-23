"""Private all-cohort quote evidence export; no model or cash-ledger replay.

Future quote quality describes the exported domain and never changes admission,
row order, or tick selection. Cash quantities are simulation algebra, not fills
or authenticated account receipts.
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

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_histogram_residual_audit as parent_audit

base = parent_audit.base
canonical = parent_audit.canonical
file_sha256 = parent_audit.file_sha256
cohort_contract = parent_audit.cohort_contract
assert_runtime_environment = parent_audit.assert_runtime_environment
UNCHANGED_FILES = parent_audit.UNCHANGED_FILES | {"analysis/offline_histogram_residual_audit.py"}
RAW_COLUMNS = [f"{kind}_{name}" for kind in ("entry", "exit") for name in base.BOOK_NUMERIC]
COLUMNS = ["market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts",
    "parent_target_status", "parent_label_available_ts", "entry_target_ts", "entry_quote_status",
    "entry_quote_ts", "entry_quote_delay_seconds", "entry_ask_price", "entry_available_shares",
    "entry_full_book_valid", "entry_size_valid", "exit_target_ts", "exit_quote_status", "exit_quote_ts",
    "exit_quote_delay_seconds", "exit_bid_price", "exit_available_shares", "exit_full_book_valid",
    "exit_size_valid", "paired_prices_observed", "paired_sizes_observed", "paired_high_quality_quotes",
    "entry_fill_price", "exit_fill_price", "entry_cash_cost_per_share", "exit_net_proceeds_per_share",
    "net_cash_difference_per_share", "entry_effective_available_shares", "exit_effective_available_shares",
    "minimum_entry_shares", "minimum_entry_cash", "maximum_pair_capacity_shares",
    "simulation_cash_domain_observed", "minimum_order_has_pair_capacity", "cash_domain_label_available_ts",
    "assumed_cash_release_ts", "cash_release_basis", *RAW_COLUMNS]
DERIVED_COLUMNS = COLUMNS[COLUMNS.index("entry_fill_price"):COLUMNS.index("maximum_pair_capacity_shares") + 1] + [
    "minimum_order_has_pair_capacity", "cash_domain_label_available_ts", "assumed_cash_release_ts"]
CASH_RELEASE_BASIS = "declared_simulation_max_market_close_plus_delay_or_exit_quote_not_authenticated_cash_receipt"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _valid_digest(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value) is not None


def _validate_cohort(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"scored_rows", "known_targets", "unknown_targets",
            "scored_ids_sha256", "known_ids_sha256", "unknown_ids_sha256"}:
        raise ValueError("invalid_cohort_binding")
    for key in ("scored_rows", "known_targets", "unknown_targets"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("invalid_cohort_count")
    if value["scored_rows"] != value["known_targets"] + value["unknown_targets"]:
        raise ValueError("inconsistent_cohort_count")
    if any(not _valid_digest(value[key]) for key in ("scored_ids_sha256", "known_ids_sha256", "unknown_ids_sha256")):
        raise ValueError("invalid_cohort_digest")


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("evaluation_mode") != "archived_quote_cash_domain_export":
        raise ValueError("export_mode_required")
    for name in ("source_commit", "expected_code_commit", "parent_code_commit"):
        if not _valid_digest(config.get(name), 40):
            raise ValueError("invalid_commit_binding")
    parent = base.validate_config(config["base_protocol"])
    if parent["source_commit"] != config["source_commit"] or digest(parent) != config.get("base_protocol_sha256"):
        raise ValueError("base_protocol_binding_mismatch")
    for name in ("expected_features_csv_sha256", "expected_coverage_sha256"):
        if not _valid_digest(config.get(name)):
            raise ValueError("invalid_artifact_binding")
    contract = config.get("export_contract", {})
    raw = contract.get("selected_raw_snapshot_fields", {})
    if (contract.get("version") != "archived_quote_cash_domain_v2" or contract.get("columns") != COLUMNS
            or raw.get("base_columns") != base.BOOK_NUMERIC or raw.get("exported_columns") != RAW_COLUMNS):
        raise ValueError("fixed_export_schema_required")
    for name in ("max_new_model_fit_calls", "max_new_model_predict_calls", "max_replay_cells", "actual_fills", "verified_canary_fills"):
        if type(config.get(name)) is not int or config[name] != 0:
            raise ValueError("zero_execution_budget_required")
    if (config.get("live_orders_enabled") is not False or config.get("new_data_collection_allowed") is not False
            or type(config.get("max_new_cloud_runs")) is not int or config["max_new_cloud_runs"] != 1
            or type(config.get("max_private_config_bytes")) is not int or config["max_private_config_bytes"] != 262144
            or len(canonical(config)) > config["max_private_config_bytes"]):
        raise ValueError("offline_export_budget_required")
    hashes = config.get("unchanged_code_file_sha256", {})
    if set(hashes) != UNCHANGED_FILES or any(not _valid_digest(v) for v in hashes.values()):
        raise ValueError("complete_frozen_code_binding_required")
    env = config.get("environment", {})
    if (not re.fullmatch(r"\d+\.\d+", str(env.get("python_major_minor", "")))
            or set(env.get("packages", {})) != set(parent_audit.PACKAGE_MODULES)
            or any(not isinstance(v, str) or not v for v in env["packages"].values())
            or any(env.get(k) is not True for k in ("must_assert_effective_get_params_exact_before_fit",
                "must_assert_runtime_versions_before_model_construction", "must_record_loaded_histogram_implementation_module_file_hashes"))):
        raise ValueError("complete_runtime_binding_required")
    _validate_cohort(config.get("expected_all_cohort"))
    _validate_cohort(config.get("expected_scored_cohort"))
    runs = config.get("expected_test_runs")
    per_run = config.get("expected_scored_cohort_per_run", {})
    if (not isinstance(runs, list) or not runs or any(not isinstance(r, str) for r in runs)
            or len(set(runs)) != len(runs) or runs != [r for r in parent["run_order"] if r in runs]
            or set(per_run) != set(runs)):
        raise ValueError("invalid_scored_run_binding")
    for value in per_run.values():
        _validate_cohort(value)
    return config


def verified_inputs(source_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    source, code = base.integrity.verified_commits(source_root, config)
    actual = {name: file_sha256(CURRENT_DIR.parent / name) for name in sorted(UNCHANGED_FILES)}
    if actual != config["unchanged_code_file_sha256"]:
        raise ValueError("frozen_code_file_binding_mismatch")
    return {"source_commit": source, "code_commit": code, "unchanged_code_file_sha256": actual}


def finite_raw(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return base.execution._time(value).isoformat()


def export_quote_row(row: pd.Series, book: pd.DataFrame | None, config: dict[str, Any]) -> dict[str, Any]:
    """Export one admitted row; selection never consults size or book quality."""
    out = dict.fromkeys(COLUMNS)
    for name in ("market_id", "run_name", "slug", "side"):
        out[name] = str(row[name])
    decision, close = base.execution._time(row["decision_ts"]), base.execution._time(row["market_close_ts"])
    out.update(decision_ts=decision.isoformat(), market_close_ts=close.isoformat(),
        parent_target_status=str(row["target_status"]), parent_label_available_ts=_timestamp(row.get("label_available_ts")),
        cash_release_basis=CASH_RELEASE_BASIS, paired_prices_observed=False, paired_sizes_observed=False,
        paired_high_quality_quotes=False, simulation_cash_domain_observed=False)
    entry_target = decision + pd.Timedelta(seconds=config["entry_delay_seconds"] + config["latency_seconds"])
    out["entry_target_ts"] = entry_target.isoformat()
    entry, reason = base.execution.select_book_quote(book, entry_target, close, side=out["side"], kind="entry",
        max_quote_delay_seconds=config["max_quote_delay_seconds"])
    out["entry_quote_status"] = reason
    out["exit_quote_status"] = "not_reached_entry_quote_unknown"

    def record(kind: str, quote: pd.Series, target: pd.Timestamp) -> None:
        stamp = base.execution._time(quote["ts_utc"])
        out[kind + "_quote_ts"] = stamp.isoformat()
        out[kind + "_quote_delay_seconds"] = float((stamp - target).total_seconds())
        for field in base.BOOK_NUMERIC:
            out[f"{kind}_{field}"] = finite_raw(quote.get(field))
        snapshot = pd.Series({field: out[f"{kind}_{field}"] for field in base.BOOK_NUMERIC})
        out[kind + "_full_book_valid"] = bool(base._valid_book(snapshot))
        action, price_name = ("buy", "ask") if kind == "entry" else ("sell", "bid")
        out[f"{kind}_{price_name}_price"] = out[f"{kind}_{action}_{out['side']}_cents"] / 100.
        size = out[f"{kind}_{action}_{out['side']}_size"]
        out[kind + "_available_shares"] = size
        out[kind + "_size_valid"] = size is not None and size >= 0

    if entry is None:
        return out
    record("entry", entry, entry_target)
    exit_target = base.execution._time(entry["ts_utc"]) + pd.Timedelta(seconds=config["hold_seconds"] + config["latency_seconds"])
    out["exit_target_ts"] = exit_target.isoformat()
    exit_quote, reason = base.execution.select_book_quote(book, exit_target, close, side=out["side"], kind="exit",
        max_quote_delay_seconds=config["max_quote_delay_seconds"])
    out["exit_quote_status"] = reason
    if exit_quote is None:
        return out
    record("exit", exit_quote, exit_target)
    out["paired_prices_observed"] = True
    out["paired_sizes_observed"] = out["entry_size_valid"] and out["exit_size_valid"]
    out["paired_high_quality_quotes"] = out["entry_full_book_valid"] and out["exit_full_book_valid"]
    p_in, p_out = out["entry_ask_price"] + config["slippage"], out["exit_bid_price"] - config["slippage"]
    observed = out["paired_high_quality_quotes"] and out["paired_sizes_observed"] and 0 < p_in < 1 and 0 < p_out < 1
    out["simulation_cash_domain_observed"] = observed
    if not observed:
        return out
    fee, haircut = config["fee_rate"], config["depth_haircut"]
    c_in, c_out = p_in + fee * p_in * (1 - p_in), p_out - fee * p_out * (1 - p_out)
    entry_capacity, exit_capacity = haircut * out["entry_available_shares"], haircut * out["exit_available_shares"]
    q_min = max(config["minimum_order_shares"], config["minimum_order_usd"] / p_in)
    q_max = min(entry_capacity, exit_capacity)
    out.update(entry_fill_price=p_in, exit_fill_price=p_out, entry_cash_cost_per_share=c_in,
        exit_net_proceeds_per_share=c_out, net_cash_difference_per_share=c_out-c_in,
        entry_effective_available_shares=entry_capacity, exit_effective_available_shares=exit_capacity,
        minimum_entry_shares=q_min, minimum_entry_cash=q_min*c_in, maximum_pair_capacity_shares=q_max,
        minimum_order_has_pair_capacity=q_max + 1e-9 >= q_min,
        cash_domain_label_available_ts=out["exit_quote_ts"],
        assumed_cash_release_ts=max(close + pd.Timedelta(seconds=config["cash_release_delay_after_close_seconds"]),
            base.execution._time(exit_quote["ts_utc"])).isoformat())
    if any(not math.isfinite(out[name]) for name in DERIVED_COLUMNS[:10]):
        raise ValueError("nonfinite_cash_domain_algebra")
    return out


def quote_export_frame(frame: pd.DataFrame, quote_index: dict[str, pd.DataFrame], config: dict[str, Any]) -> pd.DataFrame:
    if frame["market_id"].isna().any() or frame["market_id"].duplicated().any():
        raise ValueError("invalid_admitted_row_identity")
    result = pd.DataFrame([export_quote_row(row, quote_index.get(str(row["market_id"])), config)
                           for _, row in frame.iterrows()], columns=COLUMNS)
    if result["market_id"].tolist() != frame["market_id"].tolist():
        raise ValueError("export_row_identity_or_order_changed")
    return result


def gate(report: dict[str, Any], name: str, actual: Any, expected: Any) -> None:
    passed = actual == expected
    report["pre_export_gates"].append({"gate": name, "passed": passed, "actual": actual, "expected": expected})
    if not passed:
        raise ValueError(name + "_mismatch")


def evaluate(source_root: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    output.chmod(0o700)
    report = {"format": "archived-quote-cash-domain-export-v1", "evaluation_status": "export_failed",
        "export_status": "incomplete", "all_identity_gates_passed": False,
        "evidence_exported": False, "strategy_evaluated": False, "actual_model_fit_calls": 0,
        "actual_model_predict_calls": 0, "replayed_cells": 0, "actual_fills": 0, "verified_canary_fills": 0,
        "new_data_collected": False, "live_orders_enabled": False, "exported_rows": 0, "pre_export_gates": []}
    stage = "configuration"
    try:
        validate_config(config)
        report.update(protocol=config, protocol_sha256=digest(config))
        stage = "source_and_code_bindings"
        report.update(verified_inputs(source_root, config))
        report["pre_export_gates"].append({"gate": stage, "passed": True})
        stage = "runtime_environment"
        report["runtime_environment"] = assert_runtime_environment(config)
        report["implementation_modules"] = parent_audit.implementation_module_hashes()
        report["pre_export_gates"].append({"gate": stage, "passed": True})
        stage = "feature_reconstruction"
        frame, quotes, coverage = base.prepare_archive(source_root, config["base_protocol"])
        report.update(coverage=coverage, reconstructed_rows=len(frame))
        base.storage._private_csv(output / "features.csv", frame)
        base.storage._private_json(output / "coverage.json", coverage)
        gate(report, "features_csv_sha256", file_sha256(output / "features.csv"), config["expected_features_csv_sha256"])
        gate(report, "coverage_sha256", digest(coverage), config["expected_coverage_sha256"])
        stage = "cohort_reconstruction"
        if frame["market_id"].isna().any() or frame["market_id"].duplicated().any():
            raise ValueError("invalid_admitted_row_identity")
        gate(report, "all_cohort", cohort_contract(frame), config["expected_all_cohort"])
        # Membership is inherited and hash-bound; no training plan is needed.
        cohort = frame[frame["run_name"].isin(config["expected_test_runs"])].copy()
        gate(report, "scored_cohort", cohort_contract(cohort), config["expected_scored_cohort"])
        per_run = {str(run): cohort_contract(part) for run, part in cohort.groupby("run_name", sort=False)}
        gate(report, "scored_cohort_per_run", per_run, config["expected_scored_cohort_per_run"])
        gate(report, "test_runs", list(per_run), config["expected_test_runs"])
        report["all_identity_gates_passed"] = True
        stage = "all_row_quote_export"
        exported = quote_export_frame(frame, quotes, config["base_protocol"])
        gate(report, "export_row_order", digest(exported["market_id"].tolist()), digest(frame["market_id"].tolist()))
        base.storage._private_csv(output / "quote-cash-domains.csv", exported)
        report.update(evaluation_status="exported", export_status="complete", evidence_exported=True, exported_rows=len(exported),
            all_cohort=cohort_contract(frame), scored_cohort=cohort_contract(cohort), scored_cohort_per_run=per_run,
            test_runs=list(per_run), export_columns=COLUMNS,
            domain_counts={name: int(exported[name].eq(True).sum()) for name in ("paired_prices_observed",
                "paired_sizes_observed", "paired_high_quality_quotes", "simulation_cash_domain_observed", "minimum_order_has_pair_capacity")},
            quote_status_counts={kind: dict(Counter(exported[kind + "_quote_status"])) for kind in ("entry", "exit")},
            cash_release_basis=CASH_RELEASE_BASIS,
            source_clock_basis="archived_snapshot_wall_clock_not_certified_per_request_receipts")
    except Exception as exc:
        report.update(evaluation_status="export_failed", evidence_exported=False,
            failure={"stage": stage, "exception_type": type(exc).__name__, "reason": str(exc)})
    report["artifacts"] = {path.name: {"sha256": file_sha256(path), "bytes": path.stat().st_size}
                           for path in sorted(output.iterdir()) if path.is_file()}
    base.storage._private_json(output / "summary.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.config.stat().st_size > 262144:
        raise ValueError("private_input_budget_exceeded")
    report = evaluate(args.source_root, json.loads(args.config.read_text()), args.output)
    return 0 if report["evidence_exported"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
