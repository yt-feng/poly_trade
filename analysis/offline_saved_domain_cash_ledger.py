#!/usr/bin/env python3
"""Audit projected candidates against the already-saved quote-cash domains.

Selection is made from saved H695 scores and decision-time features first.  The
domain table is then used only to evaluate the selected rows against the frozen
fee, depth, minimum-order, release-time and chronological cash rules.  This is
an old-data controlled ledger diagnostic: it is neither a raw quote replay nor
an authenticated venue fill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

try:
    from analysis.timed_quote_execution import depth_proxy_fraction, entry_price_cap
    from analysis.offline_full_candidate_coverage import FEATURE_FIELDS, SCORE_FIELDS, implied_exit_bid
except ModuleNotFoundError:  # Direct CLI execution places this directory first on sys.path.
    from timed_quote_execution import depth_proxy_fraction, entry_price_cap
    from offline_full_candidate_coverage import FEATURE_FIELDS, SCORE_FIELDS, implied_exit_bid


DOMAIN_FIELDS = {
    "market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts",
    "entry_quote_status", "entry_quote_ts", "entry_ask_price", "entry_available_shares",
    "entry_full_book_valid", "entry_size_valid", "exit_quote_status", "exit_quote_ts",
    "exit_bid_price", "exit_available_shares", "exit_full_book_valid", "exit_size_valid",
    "paired_high_quality_quotes", "entry_fill_price", "exit_fill_price",
    "entry_cash_cost_per_share", "exit_net_proceeds_per_share", "net_cash_difference_per_share",
    "entry_effective_available_shares", "exit_effective_available_shares",
    "minimum_entry_shares", "minimum_entry_cash", "maximum_pair_capacity_shares",
    "simulation_cash_domain_observed", "minimum_order_has_pair_capacity",
    "cash_domain_label_available_ts", "assumed_cash_release_ts",
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
        raise ValueError(f"invalid_numeric_{field}") from exc
    if not math.isfinite(number):
        raise ValueError(f"nonfinite_numeric_{field}")
    return number


def _time(value: Any, field: str, *, nullable: bool = False) -> pd.Timestamp | None:
    if nullable and (value is None or value == ""):
        return None
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError(f"unqualified_timestamp_{field}")
    return stamp.tz_convert("UTC")


def _true(value: Any, field: str) -> bool:
    if value == "True" or value is True:
        return True
    if value == "False" or value is False:
        return False
    raise ValueError(f"invalid_boolean_{field}")


def _identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(row["market_id"]), str(row["run_name"]), str(row["slug"]), str(row["side"]),
        _time(row["decision_ts"], "decision_ts").isoformat(),
        _time(row["market_close_ts"], "market_close_ts").isoformat(),
    )


def _read(path: Path, required: set[str]) -> tuple[pd.DataFrame, str]:
    digest = _sha256(path)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name}_missing_fields:{missing}")
    return frame, digest


def _test_cohort(features: pd.DataFrame, scores: pd.DataFrame, domains: pd.DataFrame,
                 config: Mapping[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    protocol = config.get("base_protocol")
    if not isinstance(protocol, Mapping):
        raise ValueError("base_protocol_required")
    runs = protocol.get("run_order")
    minimum = protocol.get("minimum_training_runs")
    if not isinstance(runs, list) or type(minimum) is not int:
        raise ValueError("test_run_binding_invalid")
    test_runs = runs[minimum:]
    cohort = features[features["run_name"].isin(test_runs)].copy()
    if any(frame["market_id"].duplicated().any() for frame in (cohort, scores, domains)):
        raise ValueError("duplicate_market_identity")
    if not (list(cohort.market_id) == list(domains.market_id) == list(scores.market_id)):
        raise ValueError("saved_join_order_mismatch")
    if not (len(cohort) == len(domains) == len(scores)):
        raise ValueError("saved_join_row_count_mismatch")
    for left, middle, right in zip(cohort.to_dict("records"), domains.to_dict("records"), scores.to_dict("records")):
        if not (_identity(left) == _identity(middle) == _identity(right)):
            raise ValueError("saved_join_identity_mismatch")
    expected = config.get("expected_test_cohort", {})
    if isinstance(expected, Mapping) and expected.get("scored_rows") not in (None, len(cohort)):
        raise ValueError("test_cohort_binding_mismatch")
    return cohort, domains, scores


def build_report(features: pd.DataFrame, domains: pd.DataFrame, scores: pd.DataFrame,
                 config: Mapping[str, Any], *, feature_sha256: str,
                 domain_sha256: str, score_sha256: str) -> dict[str, Any]:
    cohort, domains, scores = _test_cohort(features, scores, domains, config)
    protocol = config["base_protocol"]
    fee = _number(protocol["fee_rate"], "fee_rate")
    slippage = _number(protocol["slippage"], "slippage")
    floor = _number(protocol["edge_floor"], "edge_floor")
    initial_cash = _number(protocol["initial_cash"], "initial_cash")
    minimum_order_usd = _number(protocol["minimum_order_usd"], "minimum_order_usd")
    minimum_order_shares = _number(protocol["minimum_order_shares"], "minimum_order_shares")
    depth_thresholds = tuple(_number(value, "depth_threshold") for value in protocol["depth_thresholds"])
    candidate_rows: list[dict[str, Any]] = []
    selection_rejections: dict[str, int] = {}
    for feature, score, domain in zip(cohort.to_dict("records"), scores.to_dict("records"), domains.to_dict("records")):
        raw_margin = score.get("predicted_net_margin_per_share", "")
        if raw_margin == "" or raw_margin is None:
            selection_rejections["score_missing"] = selection_rejections.get("score_missing", 0) + 1
            continue
        margin = _number(raw_margin, "predicted_net_margin_per_share")
        if margin < floor:
            selection_rejections["score_below_edge_floor"] = selection_rejections.get("score_below_edge_floor", 0) + 1
            continue
        try:
            predicted_exit_bid, reconstructed = implied_exit_bid(
                margin, feature["decision_ask_price"], fee_rate=fee, slippage=slippage)
            depth = _number(feature["decision_sell_size"], "decision_sell_size")
            if depth < 0:
                raise ValueError("negative_decision_depth")
            candidate_rows.append({"feature": feature, "score": score, "domain": domain,
                                   "predicted_exit_bid": predicted_exit_bid,
                                   "margin": margin, "inversion_error": abs(reconstructed - margin)})
        except ValueError:
            selection_rejections["invalid_execution_projection"] = selection_rejections.get("invalid_execution_projection", 0) + 1

    candidate_rows.sort(key=lambda row: (_time(row["feature"]["decision_ts"], "decision_ts"),
                                         str(row["feature"]["slug"]), str(row["feature"]["side"])))
    cash = initial_cash
    pending: tuple[pd.Timestamp, float] | None = None
    accepted = 0
    resolved = 0
    ending_cash = initial_cash
    gate_counts: dict[str, int] = {}
    realized_margins: list[float] = []
    cash_before_values: list[float] = []
    cash_after_values: list[float] = []

    def reject(reason: str) -> None:
        gate_counts[reason] = gate_counts.get(reason, 0) + 1

    for row in candidate_rows:
        feature, domain = row["feature"], row["domain"]
        decision = _time(feature["decision_ts"], "decision_ts")
        entry_target = _time(domain.get("entry_target_ts", ""), "entry_target_ts", nullable=True)
        if entry_target is None:
            # The saved domain contract normally carries this field, but keep
            # the diagnostic conservative if an older envelope omits it.
            entry_target = decision + pd.Timedelta(seconds=float(protocol["entry_delay_seconds"] + protocol["latency_seconds"]))
        if pending is not None:
            if entry_target <= pending[0]:
                reject("prior_cash_not_released")
                continue
            cash += pending[1]
            pending = None
        try:
            if not (_true(domain["simulation_cash_domain_observed"], "simulation_cash_domain_observed")
                    and _true(domain["minimum_order_has_pair_capacity"], "minimum_order_has_pair_capacity")):
                raise ValueError("domain_cash_or_minimum_gate_false")
            if domain["entry_quote_status"] != "selected" or domain["exit_quote_status"] != "selected":
                raise ValueError("quote_not_selected")
            if not (_true(domain["entry_full_book_valid"], "entry_full_book_valid")
                    and _true(domain["entry_size_valid"], "entry_size_valid")
                    and _true(domain["exit_full_book_valid"], "exit_full_book_valid")
                    and _true(domain["exit_size_valid"], "exit_size_valid")):
                raise ValueError("book_or_size_invalid")
            entry_price = _number(domain["entry_fill_price"], "entry_fill_price")
            entry_cost = _number(domain["entry_cash_cost_per_share"], "entry_cash_cost_per_share")
            exit_net = _number(domain["exit_net_proceeds_per_share"], "exit_net_proceeds_per_share")
            capacity = _number(domain["maximum_pair_capacity_shares"], "maximum_pair_capacity_shares")
            if not 0 < entry_price < 1 or entry_cost <= 0 or capacity < 0:
                raise ValueError("domain_price_or_capacity_invalid")
            limit = entry_price_cap(row["predicted_exit_bid"], edge_floor=floor, fee_rate=fee, slippage=slippage)
            if entry_price > limit + 1e-12:
                raise ValueError("entry_cost_limit_exceeded")
            fraction, _ = depth_proxy_fraction(
                _number(feature["decision_ask_price"], "decision_ask_price") + slippage,
                _number(feature["decision_sell_size"], "decision_sell_size"), cash,
                fee_rate=fee, thresholds=depth_thresholds,
            )
            gross_target = max(minimum_order_usd, cash * fraction)
            shares = max(minimum_order_shares, gross_target / entry_price)
            if shares * entry_cost > cash:
                shares = cash / entry_cost
            minimum_shares = max(minimum_order_shares, minimum_order_usd / entry_price)
            shares = min(shares, capacity)
            if shares + 1e-9 < minimum_shares:
                raise ValueError("cash_or_pair_capacity_below_minimum_order")
            release = _time(domain.get("assumed_cash_release_ts", ""), "assumed_cash_release_ts", nullable=True)
            if release is None:
                raise ValueError("cash_release_unavailable")
            payout = shares * exit_net
            before = cash
            cash -= shares * entry_cost
            pending = (release, payout)
            accepted += 1
            resolved += 1
            realized_margins.append(_number(domain["net_cash_difference_per_share"], "net_cash_difference_per_share"))
            cash_before_values.append(before)
            cash_after_values.append(cash)
        except ValueError as exc:
            reject(str(exc))

    if pending is not None:
        cash += pending[1]
        pending = None
    ending_cash = cash
    inversion_errors = [row["inversion_error"] for row in candidate_rows]
    return {
        "format": "offline-saved-domain-cash-ledger-v1",
        "scope": "saved H695 scores, features and quote-cash domains; no fit, predict, raw quote replay or authenticated fill",
        "feature_file_sha256": feature_sha256,
        "domain_file_sha256": domain_sha256,
        "score_file_sha256": score_sha256,
        "test_cohort_rows": len(cohort),
        "selected_score_candidates": len(candidate_rows),
        "selection_rejections": selection_rejections,
        "ledger_accepted_events": accepted,
        "ledger_resolved_events": resolved,
        "ledger_rejection_counts": gate_counts,
        "initial_cash": initial_cash,
        "ending_cash": ending_cash,
        "net_cash_change": ending_cash - initial_cash,
        "cash_growth_positive": ending_cash > initial_cash + 1e-9,
        "realized_domain_margin_min": min(realized_margins) if realized_margins else None,
        "realized_domain_margin_max": max(realized_margins) if realized_margins else None,
        "maximum_score_margin_inversion_absolute_error": max(inversion_errors) if inversion_errors else None,
        "required_fills": protocol["required_fills"],
        "continuous_required_sequence_complete": resolved >= protocol["required_fills"],
        "model_fit_calls": 0,
        "model_predict_calls": 0,
        "raw_quote_replays": 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--domains", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    features, feature_sha = _read(args.features, FEATURE_FIELDS)
    domains, domain_sha = _read(args.domains, DOMAIN_FIELDS)
    scores, score_sha = _read(args.scores, SCORE_FIELDS)
    report = build_report(features, domains, scores, json.loads(args.config.read_text(encoding="utf-8")),
                          feature_sha256=feature_sha, domain_sha256=domain_sha, score_sha256=score_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
