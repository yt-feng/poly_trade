"""Reuse bounded, saved quote selections on the frozen chronological cash ledger.

Saved selector outputs preserve unavailable reasons and selected raw fields. They
are not fabricated raw ticks or authenticated fills. Future full-book quality
never decides execution admission; an optional capacity forecast is supplied by
the caller as a value frozen before the entry request.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import offline_bid_residual_audit as base
import offline_side_interaction_audit as parent_audit
import timed_quote_execution as reference
try:
    from .scheduled_exit_retry import ExitRequest, ExitResolution
except ImportError:  # Standalone analysis CLI import.
    from scheduled_exit_retry import ExitRequest, ExitResolution

INITIAL_CASH = reference.INITIAL_CASH
MINIMUM_ORDER_USD = reference.MINIMUM_ORDER_USD
MINIMUM_ORDER_SHARES = reference.MINIMUM_ORDER_SHARES
REQUIRED_FILLS = reference.REQUIRED_FILLS
_number = reference._number
_time = reference._time
entry_price_cap = reference.entry_price_cap
depth_proxy_fraction = reference.depth_proxy_fraction

BOOK_FIELDS = tuple(base.BOOK_NUMERIC)
UNAVAILABLE_REASONS = frozenset({"target_at_or_after_close", "missing_book", "no_quote_in_window",
    "unknown_quote_conflict", "invalid_quote_price"})
NOT_REACHED = "not_reached_entry_quote_unknown"
IDENTITY_FIELDS = ("market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts")
CACHE_FIELDS = (*IDENTITY_FIELDS, *(f"{kind}_{name}" for kind in ("entry", "exit")
    for name in ("target_ts", "quote_status", "quote_ts", *BOOK_FIELDS)))


def _missing(value: Any) -> bool:
    return value is None or (not isinstance(value, (list, tuple, dict)) and bool(pd.isna(value)))


def _raw_number(value: Any) -> float:
    if _missing(value):
        return float("nan")
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("invalid_cached_raw_number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_cached_raw_number") from exc
    if not math.isfinite(result):
        raise ValueError("nonfinite_cached_raw_number_must_be_null")
    return result


class CachedQuoteSource:
    """Exact, request-bound access to previously selected snapshots.

    Only CACHE_FIELDS are accessed. Derived future quality, margin, minimum-size
    feasibility and outcome labels are deliberately outside this interface.
    """
    def __init__(self, domains: pd.DataFrame, protocol: Mapping[str, Any]):
        if not set(CACHE_FIELDS).issubset(domains.columns):
            raise ValueError("incomplete_cached_quote_schema")
        clocks = {name: _number(protocol[name]) for name in ("entry_delay_seconds", "latency_seconds",
            "hold_seconds", "max_quote_delay_seconds")}
        if min(clocks.values()) < 0 or clocks["hold_seconds"] <= 0:
            raise ValueError("invalid_cached_quote_clock_contract")
        self._tolerance = clocks["max_quote_delay_seconds"]
        self._records: dict[str, dict[str, Any]] = {}
        selected = domains.loc[:, list(CACHE_FIELDS)]
        for row in selected.to_dict("records"):
            identity = {}
            for name in IDENTITY_FIELDS[:4]:
                value = row[name]
                if not isinstance(value, str) or not value:
                    raise ValueError("invalid_cached_quote_identity")
                identity[name] = value
            market_id = identity["market_id"]
            if (market_id in self._records or identity["side"] not in ("up", "down")
                    or market_id != identity["run_name"] + "::" + identity["slug"]):
                raise ValueError("invalid_or_duplicate_cached_quote_identity")
            decision, close = _time(row["decision_ts"]), _time(row["market_close_ts"])
            if decision >= close:
                raise ValueError("invalid_cached_market_clock")
            entry_target = decision + pd.Timedelta(seconds=clocks["entry_delay_seconds"] + clocks["latency_seconds"])
            if _time(row["entry_target_ts"]) != entry_target:
                raise ValueError("cached_entry_target_mismatch")
            record = {**identity, "decision_ts": decision, "market_close_ts": close}
            for kind in ("entry", "exit"):
                status = row[f"{kind}_quote_status"]
                if not isinstance(status, str) or status not in UNAVAILABLE_REASONS | {"selected", NOT_REACHED}:
                    raise ValueError("invalid_cached_quote_status")
                if kind == "entry":
                    target = entry_target
                    if status == NOT_REACHED:
                        raise ValueError("entry_cannot_be_not_reached")
                elif record["entry_quote_status"] == "selected":
                    target = record["entry_quote"]["ts_utc"] + pd.Timedelta(
                        seconds=clocks["hold_seconds"] + clocks["latency_seconds"])
                    if _time(row["exit_target_ts"]) != target or status == NOT_REACHED:
                        raise ValueError("cached_exit_target_mismatch")
                else:
                    target = None
                    if status != NOT_REACHED or not _missing(row["exit_target_ts"]):
                        raise ValueError("unreached_exit_must_remain_unknown")
                record[f"{kind}_target_ts"] = target
                record[f"{kind}_quote_status"] = status
                record[f"{kind}_quote"] = None
                if status != "selected":
                    if (not _missing(row[f"{kind}_quote_ts"])
                            or any(not _missing(row[f"{kind}_{field}"]) for field in BOOK_FIELDS)):
                        raise ValueError("unselected_quote_cannot_have_selected_snapshot")
                    if target is not None and ((target >= close) != (status == "target_at_or_after_close")):
                        raise ValueError("cached_unavailable_close_reason_mismatch")
                    continue
                stamp = _time(row[f"{kind}_quote_ts"])
                if target is None or not target <= stamp <= target + pd.Timedelta(seconds=self._tolerance) or stamp >= close:
                    raise ValueError("cached_selected_quote_outside_request_window")
                values = {field: _raw_number(row[f"{kind}_{field}"]) for field in BOOK_FIELDS}
                price_field = ("buy" if kind == "entry" else "sell") + "_" + identity["side"] + "_cents"
                if not math.isfinite(values[price_field]) or not 0 < values[price_field] < 100:
                    raise ValueError("cached_selected_price_invalid")
                record[f"{kind}_quote"] = pd.Series({"ts_utc": stamp, **values})
            self._records[market_id] = record

    def select(self, market_id: str, target_ts: Any, close_ts: Any, *, side: str,
               kind: str = "entry", max_quote_delay_seconds: float = 3.) -> tuple[pd.Series | None, str]:
        if market_id not in self._records:
            raise ValueError("cached_market_missing")
        record = self._records[market_id]
        if (kind not in ("entry", "exit") or side != record["side"]
                or _time(close_ts) != record["market_close_ts"]
                or _number(max_quote_delay_seconds) != self._tolerance
                or record.get(f"{kind}_target_ts") is None
                or _time(target_ts) != record[f"{kind}_target_ts"]):
            raise ValueError("cached_quote_request_mismatch")
        quote = record[f"{kind}_quote"]
        return (quote.copy(deep=True) if quote is not None else None), record[f"{kind}_quote_status"]


def _validated_caps(candidates: pd.DataFrame, values: Mapping[str, float] | None) -> dict[str, float] | None:
    if values is None:
        return None
    if not isinstance(values, Mapping):
        raise ValueError("invalid_frozen_capacity_map")
    caps = {}
    for market_id in candidates.get("market_id", pd.Series(dtype=str)).astype(str):
        if market_id not in values:
            raise ValueError("missing_frozen_capacity_forecast")
        cap = _number(values[market_id])
        if cap < 0:
            raise ValueError("invalid_frozen_capacity_forecast")
        caps[market_id] = cap
    return caps


def regenerate_control(cohort: pd.DataFrame, control_predictions: pd.DataFrame,
                       config: Mapping[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rebuild cost intent from every cached forecast before comparing snapshots."""
    allowed = {"market_id", "run_name", "fit_run", "predicted_oriented_residual_raw",
        "predicted_bid_residual_raw", "predicted_exit_bid_unclipped", "predicted_exit_bid_price"}
    if not set(control_predictions.columns).issubset(allowed):
        raise ValueError("unexpected_control_projection_field")
    if ("market_id" not in cohort or "market_id" not in control_predictions
            or "predicted_exit_bid_price" not in control_predictions
            or cohort["market_id"].isna().any() or control_predictions["market_id"].isna().any()
            or cohort["market_id"].duplicated().any() or control_predictions["market_id"].duplicated().any()
            or cohort["market_id"].tolist() != control_predictions["market_id"].tolist()):
        raise ValueError("control_full_cohort_identity_or_order_mismatch")
    scored = cohort.copy()
    for field in control_predictions.columns:
        if field in ("market_id", "run_name"):
            if field in scored and scored[field].tolist() != control_predictions[field].tolist():
                raise ValueError("control_prediction_identity_mismatch")
            continue
        if field == "fit_run":
            if "run_name" not in scored or control_predictions[field].tolist() != scored["run_name"].tolist():
                raise ValueError("control_prediction_fit_run_mismatch")
            scored[field] = control_predictions[field].to_numpy(copy=True)
            continue
        numbers = control_predictions[field].map(_number).to_numpy(dtype=float)
        scored[field] = numbers
    candidates, decisions = base.decision_cost_candidates(scored, config["base_protocol"])
    frozen = parent_audit.frozen_candidates(dict(config))
    if len(candidates) != len(frozen):
        raise ValueError("control_candidate_count_mismatch")
    fields = parent_audit.CANDIDATE_FIELDS
    def projection(frame: pd.DataFrame) -> list[dict[str, Any]]:
        rows = []
        for values in frame[fields].to_dict("records"):
            row = {}
            for field in fields:
                if field in ("decision_ts", "market_close_ts"):
                    row[field] = _time(values[field]).isoformat()
                elif field in ("decision_ask_price", "decision_sell_size", "predicted_exit_bid_price"):
                    row[field] = _number(values[field])
                else:
                    row[field] = str(values[field])
            rows.append(row)
        return rows
    # The reference fingerprint contract normalizes floating results to ten
    # decimals; this comparison uses that same declared serialization tolerance.
    if parent_audit.normalized_result(projection(candidates)) != parent_audit.normalized_result(projection(frozen)):
        raise ValueError("control_full_candidate_projection_mismatch")
    return candidates, decisions


def replay_cached_quotes(candidates: pd.DataFrame, run_names: Sequence[str],
                         quote_source: CachedQuoteSource, *,
                         capacity_cap_by_id: Mapping[str, float] | None = None,
                         exit_resolver: Callable[..., ExitResolution] | None = None,
                         hold_seconds: float = 30.,
                        entry_delay_seconds: float = 4., latency_seconds: float = 8.,
                        max_quote_delay_seconds: float = 3., edge_floor: float = .02,
                        fee_rate: float = .07, slippage: float = .01, depth_haircut: float = .1,
                        thresholds: Sequence[float] = (3., 5., 7.),
                        cash_release_delay_after_close_seconds: float = 900.,
                        initial_cash: float = INITIAL_CASH,
                        minimum_order_usd: float = MINIMUM_ORDER_USD,
                        minimum_order_shares: float = MINIMUM_ORDER_SHARES,
                        required_fills: int = REQUIRED_FILLS) -> dict[str, Any]:
    """Replay saved selector outputs with optional cap or exit resolution.

    With neither option this retains every arithmetic operation and result field
    of the reference ledger. A cap never changes selection or minimum cash stops.
    """
    hold, entry_delay, latency, tolerance, release_delay = map(_number, (
        hold_seconds, entry_delay_seconds, latency_seconds, max_quote_delay_seconds,
        cash_release_delay_after_close_seconds))
    fee, slip, floor, haircut = map(_number, (fee_rate, slippage, edge_floor, depth_haircut))
    if hold <= 0 or min(entry_delay, latency, tolerance, release_delay) < 0 or not 0 < haircut <= 1:
        raise ValueError("invalid_execution_clock_or_depth")
    entry_price_cap(.5, edge_floor=floor, fee_rate=fee, slippage=slip)
    depth_proxy_fraction(.5, 0., INITIAL_CASH, fee_rate=fee, thresholds=thresholds)
    if (_number(initial_cash) != INITIAL_CASH or _number(minimum_order_usd) != MINIMUM_ORDER_USD
            or _number(minimum_order_shares) != MINIMUM_ORDER_SHARES
            or type(required_fills) is not int or required_fills != REQUIRED_FILLS):
        raise ValueError("frozen_ledger_contract_mismatch")
    if len(set(run_names)) != len(run_names):
        raise ValueError("duplicate_run_scope")
    if exit_resolver is not None and not callable(exit_resolver):
        raise ValueError("invalid_exit_resolver")
    caps = _validated_caps(candidates, capacity_cap_by_id)
    cash = float(initial_cash)
    chain: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    pending: tuple[pd.Timestamp, float] | None = None
    available_ts = None
    locked_shares = 0.0
    failure = None
    required = {"market_id", "run_name", "slug", "side", "decision_ts", "market_close_ts",
                "decision_ask_price", "decision_sell_size", "predicted_exit_bid_price"}
    if not candidates.empty and not required.issubset(candidates.columns):
        raise ValueError("incomplete_timed_candidate")
    ordered = candidates[candidates["run_name"].isin(run_names)].copy() if not candidates.empty else candidates
    if not ordered.empty:
        ordered["decision_ts"] = ordered["decision_ts"].map(_time)
        ordered = ordered.sort_values(["decision_ts", "slug", "side"], kind="mergesort")
    for row in ordered.itertuples(index=False):
        decision, close = _time(row.decision_ts), _time(row.market_close_ts)
        side = str(row.side)
        decision_ask, decision_depth, prediction = map(_number, (
            row.decision_ask_price, row.decision_sell_size, row.predicted_exit_bid_price))
        if side not in ("up", "down") or not 0 < decision_ask < 1 or decision_depth < 0 or decision >= close:
            raise ValueError("invalid_frozen_candidate")
        limit = entry_price_cap(prediction, edge_floor=floor, fee_rate=fee, slippage=slip)
        predicted_sell = prediction - slip
        predicted_net = predicted_sell - fee * predicted_sell * (1 - predicted_sell)
        target = decision + pd.Timedelta(seconds=entry_delay + latency)
        attempt = {"market_id": str(row.market_id), "decision_ts": decision.isoformat(),
                   "entry_target_ts": target.isoformat()}
        decision_price = decision_ask + slip
        if not 0 < decision_price < 1 or limit <= 0 or decision_price > limit + 1e-12:
            attempts.append({**attempt, "status": "decision_cost_floor_not_met",
                "decision_fill_price": decision_price, "maximum_continuous_price": limit,
                "predicted_exit_bid_price": prediction, "predicted_exit_net_per_share": predicted_net,
                "cash_unchanged": cash})
            continue
        if pending is not None:
            if target <= pending[0]:
                attempts.append({**attempt, "status": "prior_cash_not_released", "cash_unchanged": cash})
                continue
            cash += pending[1]
            available_ts, pending = pending[0], None
        entry_quote, reason = quote_source.select(str(row.market_id), target, close, side=side, kind="entry",
                                               max_quote_delay_seconds=tolerance)
        if entry_quote is None:
            attempts.append({**attempt, "status": "entry_quote_unavailable", "reason": reason, "cash_unchanged": cash})
            continue
        entry_ts = _time(entry_quote["ts_utc"])
        price = float(entry_quote[f"buy_{side}_cents"]) / 100 + slip
        if not 0 < price < 1:
            attempts.append({**attempt, "status": "invalid_entry_fill_price", "cash_unchanged": cash})
            continue
        fee_per_share = fee * price * (1 - price)
        evidence = {"predicted_exit_bid_price": prediction, "predicted_exit_fill_price": predicted_sell,
                    "predicted_exit_net_per_share": predicted_net, "edge_floor": floor, "fee_rate": fee,
                    "slippage": slip, "maximum_continuous_price": limit,
                    "arrival_net_edge": predicted_net - price - fee_per_share,
                    "basis": "frozen_future_bid_prediction_both_side_costs_continuous_price"}
        if price > limit + 1e-12 or limit <= 0:
            attempts.append({**attempt, "status": "entry_cost_limit_exceeded", "arrival_fill_price": price,
                             "entry_cost_limit_evidence": evidence, "cash_unchanged": cash})
            continue
        fraction, ratio = depth_proxy_fraction(decision_ask + slip, decision_depth, cash,
                                               fee_rate=fee, thresholds=thresholds)
        gross_target = max(minimum_order_usd, cash * fraction)
        shares = max(minimum_order_shares, gross_target / price)
        if shares * (price + fee_per_share) > cash:
            shares = cash / (price + fee_per_share)
        if shares + 1e-9 < max(minimum_order_shares, minimum_order_usd / price):
            attempts.append({**attempt, "status": "cash_cannot_fit_minimum_order", "cash_unchanged": cash})
            failure = "cash_cannot_fit_minimum_order"
            break
        cap_evidence = None
        if caps is not None:
            legacy_shares = shares
            cap = caps[str(row.market_id)]
            shares = min(legacy_shares, cap)
            minimum_shares = max(minimum_order_shares, minimum_order_usd / price)
            cap_evidence = {"frozen_at_decision_ts": decision.isoformat(),
                "forecast_exit_capacity_shares": cap, "legacy_shares": legacy_shares,
                "proposed_shares": shares, "minimum_order_shares_at_arrival": minimum_shares,
                "basis": "minimum_of_legacy_quantity_and_decision_frozen_forecast_not_observed_exit_depth"}
            if shares + 1e-9 < minimum_shares:
                attempts.append({**attempt, "status": "forecast_exit_capacity_below_minimum_order",
                    "quantity_cap_evidence": cap_evidence, "cash_unchanged": cash})
                continue
        try:
            size = float(entry_quote.get(f"buy_{side}_size", np.nan))
        except (TypeError, ValueError):
            size = float("nan")
        if not math.isfinite(size) or size < 0 or size * haircut < shares:
            attempts.append({**attempt, "status": "entry_depth_insufficient_or_invalid", "cash_unchanged": cash,
                **({"quantity_cap_evidence": cap_evidence} if cap_evidence is not None else {})})
            continue
        cost, entry_fee = shares * price, shares * fee_per_share
        before = cash
        cash -= cost + entry_fee
        available_ts = entry_ts
        exit_target = entry_ts + pd.Timedelta(seconds=hold + latency)
        event = {"market_id": str(row.market_id), "run_name": str(row.run_name), "slug": str(row.slug),
                 "side": side, "decision_ts": decision.isoformat(), "entry_ts": entry_ts.isoformat(),
                 "entry_target_ts": target.isoformat(), "market_close_ts": close.isoformat(),
                 "entry_quote_delay_seconds": (entry_ts - target).total_seconds(),
                 "shares": shares, "entry_fill_price": price, "entry_cost": cost, "entry_fee": entry_fee,
                 "cash_before_entry": before, "cash_after_entry": cash, "fraction": fraction,
                 "depth_capacity_ratio": ratio, "predicted_exit_bid_price": prediction,
                 "entry_cost_limit_evidence": evidence, "exit_target_ts": exit_target.isoformat(),
                 "entry_quote_evidence": {"observed_ts": entry_ts.isoformat(), "ask_price": price - slip,
                    "available_shares": size, "depth_haircut": haircut, "effective_available_shares": size * haircut},
                 "status": "open"}
        if cap_evidence is not None:
            event["quantity_cap_evidence"] = cap_evidence
        chain.append(event)
        exit_quote, reason = quote_source.select(str(row.market_id), exit_target, close, side=side, kind="exit",
                                              max_quote_delay_seconds=tolerance)
        if exit_resolver is not None:
            resolution = exit_resolver(ExitRequest(market_id=str(row.market_id), side=side,
                original_exit_target_ts=exit_target, market_close_ts=close, held_shares=shares,
                slippage=slip, depth_haircut=haircut, max_quote_delay_seconds=tolerance,
                order_latency_seconds=latency), exit_quote, reason, quote_source)
            if not isinstance(resolution, ExitResolution):
                raise ValueError("invalid_exit_resolution")
            exit_quote, reason = resolution.quote, resolution.reason
            if resolution.trace:
                event["exit_retry"] = resolution.to_evidence()
        if exit_quote is None:
            locked_shares = shares
            failure = "exit_quote_unavailable"
            event.update(status="unresolved_position", unresolved_reason=reason, payout=None)
            break
        exit_ts = _time(exit_quote["ts_utc"])
        sell_price = float(exit_quote[f"sell_{side}_cents"]) / 100 - slip
        try:
            sell_size = float(exit_quote.get(f"sell_{side}_size", np.nan))
        except (TypeError, ValueError):
            sell_size = float("nan")
        event["exit_quote_evidence"] = {"observed_ts": exit_ts.isoformat(),
            "bid_price": float(exit_quote[f"sell_{side}_cents"]) / 100,
            "available_shares": sell_size if math.isfinite(sell_size) else None,
            "depth_haircut": haircut, "effective_available_shares": sell_size * haircut if math.isfinite(sell_size) else None,
            "target_ts": exit_target.isoformat(), "quote_delay_seconds": (exit_ts - exit_target).total_seconds()}
        if not 0 < sell_price < 1 or not math.isfinite(sell_size) or sell_size < 0 or sell_size * haircut < shares:
            locked_shares = shares
            failure = "exit_price_or_depth_invalid"
            event.update(status="unresolved_position", unresolved_reason=failure, payout=None)
            break
        exit_gross = shares * sell_price
        exit_fee = shares * fee * sell_price * (1 - sell_price)
        payout = exit_gross - exit_fee
        release = max(close + pd.Timedelta(seconds=release_delay), exit_ts)
        pending = (release, payout)
        event.update(status="timed_quote_exit", exit_ts=exit_ts.isoformat(), exit_fill_price=sell_price,
                     exit_gross_proceeds=exit_gross, exit_fee=exit_fee, payout=payout,
                     realized_pnl=payout - cost - entry_fee, cash_release_ts=release.isoformat())
        if len(chain) >= required_fills:
            break
    if pending is not None:
        cash += pending[1]
        available_ts = pending[0]
    resolved = sum(event["status"] == "timed_quote_exit" for event in chain)
    complete = len(chain) == required_fills and resolved == required_fills and locked_shares == 0
    passed = complete and cash > initial_cash + 1e-9
    if failure is None and not passed:
        failure = "no_candidates" if candidates.empty else "sequence_or_net_growth_gate_not_met"
    return {"fills": len(chain), "resolved_fills": resolved, "ending_cash": cash,
            "first_ten_fills": len(chain), "first_ten_resolved_fills": resolved,
            "first_ten_ending_cash": cash, "first_ten_passed": bool(passed),
            "strict_net_growth_ten": bool(passed), "ten_resolved_sequence_complete": bool(complete),
            "failure_reason": failure, "locked_shares": locked_shares,
            "ending_cash_available_ts": available_ts.isoformat() if available_ts is not None else None,
            "chain": chain, "attempts": attempts,
            "execution_class": "fixed_horizon_quote_simulation_not_authenticated_fill",
            "cash_release_basis": "assumed_close_plus_declared_delay_or_later_exit",
            "sizing_basis": ("frozen_legacy_depth_ratio_mapping_not_kelly_optimum" if caps is None else
                "frozen_legacy_depth_ratio_mapping_with_decision_frozen_capacity_cap_not_kelly_optimum"),
            "actual_fills": 0, "verified_canary_fills": 0, "live_orders_enabled": False}


def replay_arm(candidates: pd.DataFrame, source: CachedQuoteSource, parent: Mapping[str, Any], *,
               capacity_cap_by_id: Mapping[str, float] | None = None) -> list[dict[str, Any]]:
    """Return the original pooled and disjoint diagnostic scope wrappers."""
    tests = parent["run_order"][parent["minimum_training_runs"]:]
    blocks = base.disjoint_blocks(tests, parent["evaluation_block_runs"])
    results = []
    for index, runs in enumerate([tests] + blocks):
        result = replay_cached_quotes(candidates, runs, source, capacity_cap_by_id=capacity_cap_by_id,
            hold_seconds=parent["hold_seconds"], entry_delay_seconds=parent["entry_delay_seconds"],
            latency_seconds=parent["latency_seconds"], max_quote_delay_seconds=parent["max_quote_delay_seconds"],
            edge_floor=parent["edge_floor"], fee_rate=parent["fee_rate"], slippage=parent["slippage"],
            depth_haircut=parent["depth_haircut"], thresholds=tuple(parent["depth_thresholds"]),
            cash_release_delay_after_close_seconds=parent["cash_release_delay_after_close_seconds"],
            initial_cash=parent["initial_cash"], minimum_order_usd=parent["minimum_order_usd"],
            minimum_order_shares=parent["minimum_order_shares"], required_fills=parent["required_fills"])
        results.append({"scope": "pooled_single_ledger" if index == 0 else "disjoint_diagnostic_block", "block_index": index,
            "runs": runs, "slippage": parent["slippage"], "latency_seconds": parent["latency_seconds"],
            "depth_haircut": parent["depth_haircut"], **result})
    return results
