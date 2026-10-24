#!/usr/bin/env python3
"""Expand saved net-margin scores into an aggregate execution-field diagnostic.

The H695 score file contains a signed net-cash-margin forecast for every test
row, while the frozen candidate file contains the execution fields.  This
module joins the already-saved score and feature bytes and algebraically
recovers the implied exit bid from the margin.  It does not fit, predict,
replay quotes, or admit an order.  The report is aggregate-only and never
contains market identities.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


SCORE_FIELDS = {
    "market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts",
    "fit_run", "model_status", "predicted_net_margin_per_share",
}
FEATURE_FIELDS = {
    "market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts",
    "decision_ask_price", "decision_sell_size",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field: {field}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite numeric field: {field}")
    return number


def _timestamp(value: Any, field: str) -> str:
    try:
        stamp = pd.Timestamp(value)
    except Exception as exc:  # pandas raises more than one parser exception
        raise ValueError(f"invalid timestamp field: {field}") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {field}")
    return stamp.tz_convert("UTC").isoformat()


def implied_exit_bid(
    margin: float,
    decision_ask_price: float,
    *,
    fee_rate: float,
    slippage: float,
) -> tuple[float, float]:
    """Invert the frozen per-share net-cash equation.

    ``p_in = ask + slippage`` and ``p_out = exit_bid - slippage``.  The
    equation is ``margin = p_out - fee*p_out*(1-p_out) -
    (p_in + fee*p_in*(1-p_in))``.  The positive root is unique on the frozen
    price interval because ``fee < 1``.
    """
    margin = _number(margin, "predicted_net_margin_per_share")
    ask = _number(decision_ask_price, "decision_ask_price")
    fee = _number(fee_rate, "fee_rate")
    slip = _number(slippage, "slippage")
    if not 0 < fee < 1 or not 0 <= slip < 1 or not 0 < ask + slip < 1:
        raise ValueError("invalid_frozen_price_parameters")
    p_in = ask + slip
    entry_cost = p_in + fee * p_in * (1.0 - p_in)
    target = entry_cost + margin
    discriminant = (1.0 - fee) ** 2 + 4.0 * fee * target
    if not math.isfinite(discriminant) or discriminant < 0:
        raise ValueError("implied_exit_discriminant_invalid")
    p_out = (-(1.0 - fee) + math.sqrt(discriminant)) / (2.0 * fee)
    exit_bid = p_out + slip
    if not 0 < p_out < 1 or not 0 < exit_bid < 1:
        raise ValueError("implied_exit_price_out_of_range")
    reconstructed = p_out - fee * p_out * (1.0 - p_out) - entry_cost
    return exit_bid, reconstructed


def _identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(row["market_id"]), str(row["run_name"]), str(row["slug"]), str(row["side"]),
        _timestamp(row["decision_ts"], "decision_ts"),
        _timestamp(row["market_close_ts"], "market_close_ts"),
    )


def _read_csv(path: Path, required: set[str]) -> tuple[pd.DataFrame, str]:
    digest = _sha256(path)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name} missing fields: {missing}")
    return frame, digest


def build_report(
    features: pd.DataFrame,
    scores: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    feature_sha256: str,
    score_sha256: str,
) -> dict[str, Any]:
    protocol = config.get("base_protocol")
    if not isinstance(protocol, Mapping):
        raise ValueError("base_protocol_required")
    run_order = protocol.get("run_order")
    minimum_training_runs = protocol.get("minimum_training_runs")
    if not isinstance(run_order, list) or type(minimum_training_runs) is not int:
        raise ValueError("invalid_test_run_binding")
    test_runs = run_order[minimum_training_runs:]
    cohort = features[features["run_name"].isin(test_runs)].copy()
    if cohort["market_id"].duplicated().any() or scores["market_id"].duplicated().any():
        raise ValueError("duplicate_market_identity")
    if list(cohort["market_id"]) != list(scores["market_id"]):
        raise ValueError("saved_score_feature_identity_or_order_mismatch")
    if len(cohort) != len(scores):
        raise ValueError("saved_score_feature_row_count_mismatch")
    for left, right in zip(cohort.to_dict("records"), scores.to_dict("records")):
        if _identity(left) != _identity(right):
            raise ValueError("saved_score_feature_identity_mismatch")

    fee_rate = _number(protocol.get("fee_rate"), "fee_rate")
    slippage = _number(protocol.get("slippage"), "slippage")
    edge_floor = _number(protocol.get("edge_floor"), "edge_floor")
    rows_with_margin = 0
    rows_at_floor = 0
    rows_with_projection = 0
    rows_at_floor_with_projection = 0
    rows_with_nonnegative_depth = 0
    inversion_errors: list[float] = []
    exit_prices: list[float] = []
    invalid_projection = 0
    score_status_counts: dict[str, int] = {}
    for feature, score in zip(cohort.to_dict("records"), scores.to_dict("records")):
        status = str(score.get("model_status", ""))
        score_status_counts[status] = score_status_counts.get(status, 0) + 1
        raw_margin = score.get("predicted_net_margin_per_share", "")
        if raw_margin == "" or raw_margin is None:
            continue
        margin = _number(raw_margin, "predicted_net_margin_per_share")
        rows_with_margin += 1
        at_floor = margin >= edge_floor
        rows_at_floor += int(at_floor)
        try:
            exit_bid, reconstructed = implied_exit_bid(
                margin, feature["decision_ask_price"], fee_rate=fee_rate, slippage=slippage
            )
            inversion_errors.append(abs(reconstructed - margin))
            exit_prices.append(exit_bid)
            _number(feature["decision_sell_size"], "decision_sell_size")
            if float(feature["decision_sell_size"]) < 0:
                raise ValueError("negative_decision_sell_size")
            rows_with_projection += 1
            rows_at_floor_with_projection += int(at_floor)
            rows_with_nonnegative_depth += 1
        except ValueError:
            invalid_projection += 1

    expected_rows = None
    expected = config.get("expected_test_cohort")
    if isinstance(expected, Mapping):
        expected_rows = expected.get("scored_rows")
        if expected_rows is not None and len(cohort) != expected_rows:
            raise ValueError("test_cohort_binding_mismatch")
    return {
        "format": "offline-full-candidate-coverage-v1",
        "scope": "saved H695 score and feature bytes; no fit, predict, quote replay, cash ledger or order admission",
        "feature_file_sha256": feature_sha256,
        "score_file_sha256": score_sha256,
        "test_cohort_rows": len(cohort),
        "expected_test_cohort_rows": expected_rows,
        "score_rows": len(scores),
        "score_status_counts": score_status_counts,
        "score_rows_with_finite_margin": rows_with_margin,
        "score_rows_at_or_above_frozen_edge_floor": rows_at_floor,
        "score_rows_with_execution_projection": rows_with_projection,
        "score_rows_at_or_above_floor_with_execution_projection": rows_at_floor_with_projection,
        "score_rows_at_or_above_floor_missing_execution_projection": rows_at_floor - rows_at_floor_with_projection,
        "projection_rows_with_nonnegative_decision_depth": rows_with_nonnegative_depth,
        "invalid_projection_rows": invalid_projection,
        "implied_exit_bid_min": min(exit_prices) if exit_prices else None,
        "implied_exit_bid_max": max(exit_prices) if exit_prices else None,
        "maximum_margin_inversion_absolute_error": max(inversion_errors) if inversion_errors else None,
        "fee_rate": fee_rate,
        "slippage": slippage,
        "edge_floor": edge_floor,
        "model_fit_calls": 0,
        "model_predict_calls": 0,
        "strategy_replays": 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    features, feature_sha = _read_csv(args.features, FEATURE_FIELDS)
    scores, score_sha = _read_csv(args.scores, SCORE_FIELDS)
    report = build_report(
        features, scores, json.loads(args.config.read_text(encoding="utf-8")),
        feature_sha256=feature_sha, score_sha256=score_sha,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
