"""Fixed-horizon quote simulation on one evolving cash ledger.

Predictions and entry limits are frozen at decision time. Quote selection never
searches for profit or sufficient depth. These records are simulated executions,
not authenticated fills or observations of reusable cash.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


INITIAL_CASH = 10.0
MINIMUM_ORDER_USD = 5.0
MINIMUM_ORDER_SHARES = 5.0
REQUIRED_FILLS = 10


def _number(value: Any) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("invalid_numeric_contract")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_numeric_contract")
    return number


def _time(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("invalid_utc_clock")
    return timestamp.tz_convert("UTC")


def select_book_quote(book: pd.DataFrame | None, target_ts: Any, close_ts: Any, *,
                      side: str, kind: str = "entry",
                      max_quote_delay_seconds: float = 3.0) -> tuple[pd.Series | None, str]:
    """Select the first valid price in a bounded arrival window.

    A conflict encountered before selection is unknown; it cannot be skipped.
    Ordinary invalid prices may wait for the next tick within the same window.
    Sizes and future returns never determine which tick is selected.
    """
    delay = _number(max_quote_delay_seconds)
    if side not in ("up", "down") or kind not in ("entry", "exit") or delay < 0:
        raise ValueError("invalid_quote_selection_contract")
    target, close = _time(target_ts), _time(close_ts)
    if target >= close:
        return None, "target_at_or_after_close"
    if book is None or book.empty:
        return None, "missing_book"
    if "ts_utc" not in book:
        raise ValueError("missing_quote_clock")
    times = pd.to_datetime(book["ts_utc"], utc=True, errors="coerce")
    mask = (times >= target) & (times <= target + pd.Timedelta(seconds=delay)) & (times < close)
    selected = book.loc[mask].copy()
    if selected.empty:
        return None, "no_quote_in_window"
    selected["ts_utc"] = times.loc[mask].array
    selected = selected.sort_values("ts_utc", kind="mergesort")
    price_field = ("buy" if kind == "entry" else "sell") + "_" + side + "_cents"
    for _, quote in selected.iterrows():
        conflict = quote.get("quote_conflict", False)
        if not isinstance(conflict, (bool, np.bool_)) or bool(conflict):
            return None, "unknown_quote_conflict"
        try:
            price = float(quote.get(price_field, np.nan))
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and 0 < price < 100:
            return quote, "selected"
    return None, "invalid_quote_price"


def entry_price_cap(predicted_exit_bid_price: float, *, edge_floor: float = .02,
                    fee_rate: float = .07, slippage: float = .01) -> float:
    """Continuous buy-fill cap after both predicted exit costs and entry fee."""
    predicted, floor, fee, slip = map(_number, (predicted_exit_bid_price, edge_floor, fee_rate, slippage))
    if not 0 <= predicted <= 1 or not 0 <= floor < 1 or not 0 <= fee <= 1 or not 0 <= slip < 1:
        raise ValueError("invalid_entry_limit_contract")
    sell_price = predicted - slip
    if sell_price <= 0:
        return 0.0
    budget = max(0.0, sell_price - fee * sell_price * (1 - sell_price) - floor)
    if fee == 0:
        return budget
    linear = 1 + fee
    return 2 * budget / (linear + math.sqrt(linear * linear - 4 * fee * budget))


def depth_proxy_fraction(decision_fill_price: float, decision_sell_size: float, cash: float, *,
                         fee_rate: float = .07,
                         thresholds: Sequence[float] = (3., 5., 7.)) -> tuple[float, float]:
    """Frozen legacy depth/full-cash-share map; not an optimal Kelly fraction."""
    price, depth, cash, fee = map(_number, (decision_fill_price, decision_sell_size, cash, fee_rate))
    tiers = tuple(map(_number, thresholds))
    if len(tiers) != 3 or not 0 < tiers[0] < tiers[1] < tiers[2] or depth < 0 or cash < -1e-9 or not 0 <= fee <= 1:
        raise ValueError("invalid_depth_fraction_contract")
    if cash < MINIMUM_ORDER_USD - 1e-12 or not 0 < price < 1:
        return .50, 0.0
    # Same full-cash sizing arithmetic as the existing compound execution map.
    shares = max(MINIMUM_ORDER_SHARES, max(MINIMUM_ORDER_USD, cash) / price)
    reserved = shares * price + shares * fee * price * (1 - price)
    if reserved > cash + 1e-12:
        per_share = price + fee * price * (1 - price)
        shares = min(shares, cash / per_share)
        reserved = shares * per_share
    if shares < MINIMUM_ORDER_SHARES - 1e-9 or shares * price < MINIMUM_ORDER_USD - 1e-9 or reserved > cash + 1e-9:
        return .50, 0.0
    ratio = depth / shares if shares > 0 else 0.0
    fraction = 1.0 if ratio >= tiers[2] else .90 if ratio >= tiers[1] else .75 if ratio >= tiers[0] else .50
    return fraction, ratio


def replay_timed_quotes(candidates: pd.DataFrame, run_names: Sequence[str],
                        quotes: Mapping[str, pd.DataFrame], *, hold_seconds: float = 30.,
                        entry_delay_seconds: float = 4., latency_seconds: float = 8.,
                        max_quote_delay_seconds: float = 3., edge_floor: float = .02,
                        fee_rate: float = .07, slippage: float = .01, depth_haircut: float = .1,
                        thresholds: Sequence[float] = (3., 5., 7.),
                        cash_release_delay_after_close_seconds: float = 900.,
                        initial_cash: float = INITIAL_CASH,
                        minimum_order_usd: float = MINIMUM_ORDER_USD,
                        minimum_order_shares: float = MINIMUM_ORDER_SHARES,
                        required_fills: int = REQUIRED_FILLS) -> dict[str, Any]:
    """Execute one bounded fixed-horizon path without using a terminal label."""
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
        book = quotes.get(str(row.market_id))
        entry_quote, reason = select_book_quote(book, target, close, side=side, kind="entry",
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
        try:
            size = float(entry_quote.get(f"buy_{side}_size", np.nan))
        except (TypeError, ValueError):
            size = float("nan")
        if not math.isfinite(size) or size < 0 or size * haircut < shares:
            attempts.append({**attempt, "status": "entry_depth_insufficient_or_invalid", "cash_unchanged": cash})
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
        chain.append(event)
        exit_quote, reason = select_book_quote(book, exit_target, close, side=side, kind="exit",
                                              max_quote_delay_seconds=tolerance)
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
            "sizing_basis": "frozen_legacy_depth_ratio_mapping_not_kelly_optimum",
            "actual_fills": 0, "verified_canary_fills": 0, "live_orders_enabled": False}
