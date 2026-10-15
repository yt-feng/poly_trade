"""Bounded, quality-first research with a cash-constrained quote replay.

All fills are quote proxies. This module has no order, account, or signing API.
Empirical outputs and frozen models belong in the sealed research vault.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

from quality_first_data import QualityPolicy, build_quality_dataset, load_verified_snapshots, mark_evaluation_scope

VERSION = 1
HOLD_MS = 30_000
FEATURES = {
    "book_residual": ["price", "uncertainty", "ttl", "spread", "imbalance"],
    "spot_residual": ["price", "uncertainty", "ttl", "spread", "imbalance", "spot5"],
}
# Fixed small comparison set, recorded before data access. No adaptive grid search.
THRESHOLDS = (0.01, 0.02)
MIN_TRAIN_MARKETS = 30
MIN_VALIDATION_CYCLES = 10


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def protocol_contract():
    """Bind frozen evaluations to both policy and its exact implementation."""
    here = Path(__file__).resolve()
    return {"version": VERSION, "quality_policy": asdict(QualityPolicy()), "features": FEATURES,
            "hold_ms": HOLD_MS, "thresholds": THRESHOLDS, "latencies_ms": [1000, 3000],
            "cash_delay_ms": 1000, "maximum_positions": 1, "fill_model": "taker_quote_proxy",
            "implementation_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in (here, here.with_name("quality_first_data.py"))}}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def rules(row):
    return (row.get("microstructure") or {}).get("rules") or {}


def fee(row, price, shares=1):
    """Observed contemporaneous USDC fee model; round conservatively per order."""
    spec = rules(row).get("fees") or {}
    rate = number(spec.get("rate"))
    stamp = number(rules(row).get("metadata_received_ms"))
    if (not spec.get("known") or rate is None or not 0 <= rate <= 1
            or number(spec.get("exponent")) != 1 or stamp is None
            or not 0 <= row["sample_ms"] - stamp <= 60_000):
        return None
    continuous = shares * rate * price * (1 - price)
    return max(continuous, round(continuous, 5))


def quote_valid(row, side, age_ms=1500):
    book = row.get(side) or {}
    bid, ask = number(book.get("bid")), number(book.get("ask"))
    received = number(book.get("received_ms"))
    return (bool(row.get("poly_valid")) and received is not None
            and 0 <= row["sample_ms"] - received <= age_ms
            and bid is not None and ask is not None and 0 < bid <= ask < 1)


def feature_vector(decision, side, family):
    row = decision["raw"]
    book = row.get(side) or {}
    if not quote_valid(row, side):
        return None
    bid_size, ask_size = number(book.get("bid_size")), number(book.get("ask_size"))
    if bid_size is None or ask_size is None or min(bid_size, ask_size) <= 0:
        return None
    mid = (book["bid"] + book["ask"]) / 2
    ttl = (decision["window_start_ms"] + 300_000 - decision["sample_ms"]) / 300_000
    values = [mid, 4 * mid * (1 - mid), ttl, (book["ask"] - book["bid"]) * 10,
              (bid_size - ask_size) / (bid_size + ask_size)]
    if family == "spot_residual":
        spot = number(decision.get("features", {}).get("spot_return_5s_bps"))
        if spot is None:
            return None
        values.append(max(-10., min(10., spot / 10)) * (1 if side == "up" else -1))
    return values


def decision_eligible(decision):
    return bool(decision.get("quality", {}).get("decision_eligible", False))


def solve(matrix, vector):
    """Small deterministic pivoted solve; no heavy local compute dependency."""
    n = len(vector)
    a = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda j: abs(a[j][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular regression")
        a[col], a[pivot] = a[pivot], a[col]
        denominator = a[col][col]
        a[col] = [v / denominator for v in a[col]]
        for row in range(n):
            if row != col:
                scale = a[row][col]
                a[row] = [x - scale * y for x, y in zip(a[row], a[col])]
    return [row[-1] for row in a]


def fit_ridge(examples, family, fit_cutoff_ms):
    usable = [x for x in examples if x["available_ms"] <= fit_cutoff_ms]
    markets = {x["slug"] for x in usable}
    if len(markets) < MIN_TRAIN_MARKETS or len(usable) < 100:
        return None
    dims = len(FEATURES[family]) + 1
    gram = [[0.] * dims for _ in range(dims)]
    rhs = [0.] * dims
    counts = Counter(x["slug"] for x in usable)
    for item in usable:
        # Equal total weight for each market; ticks are not independent markets.
        x = [1.] + item["x"]
        weight = 1 / counts[item["slug"]]
        for i in range(dims):
            rhs[i] += weight * x[i] * item["y"]
            for j in range(dims):
                gram[i][j] += weight * x[i] * x[j]
    for i in range(1, dims):
        gram[i][i] += 1.0
    gram[0][0] += 1e-8
    return {"family": family, "features": FEATURES[family], "coef": solve(gram, rhs),
            "fit_cutoff_ms": fit_cutoff_ms, "training_markets": len(markets),
            "training_examples": len(usable), "label": "received_quote_mid_change_30s",
            "target_is_official_resolution": False, "ridge_penalty": 1.0}


def predict(model, vector):
    return sum(a * b for a, b in zip(model["coef"], [1.] + vector))


def next_quote(decisions, times, target_ms, side, slug, tolerance_ms=2500):
    i = bisect.bisect_left(times, target_ms)
    while i < len(decisions) and times[i] <= target_ms + tolerance_ms:
        row = decisions[i]["raw"]
        if row["slug"] != slug:
            return None
        received = number((row.get(side) or {}).get("received_ms"))
        if received is not None and target_ms <= received <= times[i] and quote_valid(row, side):
            return i
        i += 1
    return None


def continuous(decisions, start, end):
    return all(0 < decisions[j]["sample_ms"] - decisions[j - 1]["sample_ms"] <= 2500
               and decisions[j]["slug"] == decisions[start]["slug"]
               for j in range(start + 1, end + 1))


def training_examples(dataset, train_end_ms, family):
    decisions = dataset["decisions"]
    times = [d["sample_ms"] for d in decisions]
    gold = set(dataset["gold_window_ids"])
    result = []
    last_sample = {}
    for i, decision in enumerate(decisions):
        if (decision["slug"] not in gold or not decision.get("training_gold") or not decision_eligible(decision)
                or decision["window_start_ms"] + 300_000 > train_end_ms - 60_000):
            continue
        now = decision["sample_ms"]
        if now - last_sample.get(decision["slug"], -10**15) < 10_000:
            continue
        last_sample[decision["slug"]] = now
        for side in ("up", "down"):
            x = feature_vector(decision, side, family)
            j = next_quote(decisions, times, now + HOLD_MS, side, decision["slug"])
            if (x is None or j is None or times[j] > train_end_ms or not continuous(decisions, i, j)
                    or not decisions[j].get("training_gold")
                    or decisions[j].get("gold_segment_id") != decision.get("gold_segment_id")):
                continue
            future = decisions[j]["raw"][side]
            current = decision["raw"][side]
            result.append({"x": x, "y": (future["bid"] + future["ask"] - current["bid"] - current["ask"]) / 2,
                           "available_ms": times[j], "slug": decision["slug"]})
    return result


def minimum_shares(decision, price=None):
    """Execution accepts only an explicitly evidenced share unit."""
    minimum = number(decision.get("min_order_size"))
    unit = decision.get("min_order_size_unit")
    if unit != "shares" or minimum is None or minimum <= 0:
        return None
    if price is not None and (not math.isfinite(price) or price <= 0):
        return None
    constraints = decision.get("rules") or {}
    notional = number(constraints.get("min_order_notional"))
    if price is not None and notional is not None and notional > 0:
        minimum = max(minimum, notional / price)
    step = number(constraints.get("order_size_step")) or 1.
    return math.ceil((minimum - 1e-12) / step) * step


def replay(decisions, model, threshold, initial_cash=10., latency_ms=1000,
           extra_exit_ticks=0, begin_ms=0, end_ms=None, cash_delay_ms=1000):
    """One pending intent/position, one attempt per market, no cash reuse early.

    Looking up subsequent quotes models execution only. Every selected intent
    stays in the ledger, including missing entry/exit observations. A missing
    exit keeps its cost locked and stops further exposure under this policy.
    """
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("positive initial cash required")
    if (latency_ms < 0 or cash_delay_ms < 0 or extra_exit_ticks < 0
            or not all(math.isfinite(v) for v in (latency_ms, cash_delay_ms, extra_exit_ticks, threshold))):
        raise ValueError("nonnegative finite execution parameters required")
    times = [d["sample_ms"] for d in decisions]
    observation_end = min(times[-1] if times else 0, end_ms if end_ms is not None else math.inf)
    used = set()
    attempts, ledger = [], []
    cash = initial_cash
    busy_until = -1
    locked = 0.
    blocks = Counter()
    for i, decision in enumerate(decisions):
        now = decision["sample_ms"]
        if decision["window_start_ms"] is None or not decision.get("evaluation_scope_included", True):
            blocks["outside_predefined_sampling_scope"] += 1
            continue
        if decision["window_start_ms"] < begin_ms or (end_ms is not None and decision["window_start_ms"] + 300_000 > end_ms):
            continue
        if now < busy_until:
            blocks["pending_or_locked_position"] += 1
            continue
        if decision["slug"] in used or not decision_eligible(decision):
            continue
        if decision["window_start_ms"] + 300_000 - now < HOLD_MS + 2 * latency_ms + 5000:
            continue
        if model is None:
            continue
        row = decision["raw"]
        tick = number(rules(row).get("tick_size"))
        shares = minimum_shares(decision)
        if shares is None:
            blocks["minimum_order_unit_unknown"] += 1
            continue
        if tick is None or not 0 < tick < 1:
            blocks["tick_unknown"] += 1
            continue
        candidates = []
        for side in ("up", "down"):
            x = feature_vector(decision, side, model["family"])
            if x is None:
                continue
            book = row[side]
            cost_fee = fee(row, book["ask"])
            exit_fee = fee(row, book["bid"])
            if cost_fee is None or exit_fee is None:
                continue
            edge = predict(model, x) - (book["ask"] - book["bid"]) - cost_fee - exit_fee - tick
            if edge >= threshold:
                candidates.append((edge, side))
        if not candidates:
            continue
        edge, side = max(candidates)
        shares = minimum_shares(decision, row[side]["ask"])
        used.add(decision["slug"])
        item = {"slug": decision["slug"], "decision_ms": now, "side": side, "shares": shares,
                "predicted_net_edge": edge, "status": "no_entry_quote", "quote_proxy": True,
                "pnl_lower_bound_usd": 0., "latency_ms": latency_ms}
        attempts.append(item)
        ceiling = min(1 - tick, row[side]["ask"] + tick)
        reserve_fee = fee(row, ceiling, shares)
        reserve = shares * ceiling + reserve_fee
        if reserve > cash + 1e-9:
            item["status"] = "insufficient_cash_for_minimum"
            continue
        entry = next_quote(decisions, times, now + latency_ms, side, decision["slug"])
        busy_until = now + latency_ms + 2500
        if entry is None:
            continue
        erow = decisions[entry]["raw"]
        busy_until = times[entry]
        if not continuous(decisions, i, entry):
            item["status"] = "entry_path_gap"
            continue
        if not decision_eligible(decisions[entry]):
            item["status"] = "entry_execution_quality"
            continue
        current_min = minimum_shares(decisions[entry], erow[side]["ask"])
        entry_tick = number(rules(erow).get("tick_size"))
        if entry_tick is None or not 0 < entry_tick < 1 or abs(ceiling / entry_tick - round(ceiling / entry_tick)) > 1e-7:
            item["status"] = "entry_tick_changed_or_invalid_limit"
            continue
        book = erow[side]
        current_step = number(decisions[entry].get("rules", {}).get("order_size_step"))
        if current_min is None or current_min > shares + 1e-9 or (current_step is not None
                and abs(shares / current_step - round(shares / current_step)) > 1e-7):
            item["status"] = "entry_minimum_changed"
            continue
        if (number(book.get("ask_size")) or 0) < shares:
            item["status"] = "entry_capacity"
            continue
        if book["ask"] > ceiling + 1e-10:
            item["status"] = "entry_limit_reject"
            continue
        entry_fee = fee(erow, book["ask"], shares)
        if entry_fee is None:
            item["status"] = "entry_fee_unknown"
            continue
        cost = shares * book["ask"] + entry_fee
        if cost > cash + 1e-9:
            item["status"] = "insufficient_cash_at_entry"
            continue
        cash -= cost
        locked += cost
        item.update(status="exit_unobserved", entry_ms=times[entry], entry_price=book["ask"],
                    entry_fee=entry_fee, cost_usd=cost, pnl_lower_bound_usd=-cost)
        ledger.append({"ms": times[entry], "kind": "proxy_entry", "cash_delta": -cost, "available_cash": cash})
        busy_until = math.inf
        out = next_quote(decisions, times, times[entry] + HOLD_MS + latency_ms, side, decision["slug"])
        if out is None:
            continue
        xrow = decisions[out]["raw"]
        if not decision_eligible(decisions[out]):
            item["status"] = "exit_execution_quality"
            continue
        if not continuous(decisions, entry, out):
            item["status"] = "exit_path_gap"
            continue
        xbook = xrow[side]
        exit_step = number(decisions[out].get("rules", {}).get("order_size_step"))
        if exit_step is not None and abs(shares / exit_step - round(shares / exit_step)) > 1e-7:
            item["status"] = "exit_size_step_changed"
            continue
        if (number(xbook.get("bid_size")) or 0) < shares:
            item["status"] = "exit_capacity_unobserved"
            continue
        xtick = number(rules(xrow).get("tick_size"))
        if xtick is None or not 0 < xtick < 1:
            item["status"] = "exit_tick_unknown"
            continue
        price = max(0., xbook["bid"] - extra_exit_ticks * xtick)
        exit_min = minimum_shares(decisions[out], price)
        if exit_min is None or exit_min > shares + 1e-9:
            item["status"] = "exit_minimum_changed"
            continue
        exit_fee = fee(xrow, price, shares)
        if exit_fee is None:
            item["status"] = "exit_fee_unknown"
            continue
        proceeds = shares * price - exit_fee
        available_ms = times[out] + cash_delay_ms
        if available_ms > observation_end:
            item.update(status="exit_observed_cash_pending", exit_ms=times[out],
                        cash_available_ms=available_ms, pending_receivable_usd=proceeds)
            continue
        cash += proceeds
        locked -= cost
        busy_until = available_ms
        item.update(status="closed_proxy_cycle", exit_ms=times[out], cash_available_ms=busy_until,
                    exit_price=price, exit_fee=exit_fee, proceeds_usd=proceeds,
                    pnl_usd=proceeds - cost, pnl_lower_bound_usd=proceeds - cost)
        ledger.append({"ms": busy_until, "kind": "proxy_exit_cash", "cash_delta": proceeds, "available_cash": cash})
    entered = [a for a in attempts if "entry_ms" in a]
    closed = [a for a in entered if a["status"] == "closed_proxy_cycle"]
    pnls = [a["pnl_lower_bound_usd"] for a in entered]
    days = defaultdict(float)
    for attempt in entered:
        days[datetime.fromtimestamp(attempt["entry_ms"] / 1000, timezone.utc).date().isoformat()] += attempt["pnl_lower_bound_usd"]
    daily_lower = None
    if len(days) >= 7:
        rng = random.Random(20261015)
        daily = list(days.values())
        draws = sorted(statistics.mean(rng.choices(daily, k=len(daily))) for _ in range(1000))
        daily_lower = draws[49]
    net = cash - initial_cash
    summary = {"initial_cash": initial_cash, "available_cash_end": cash, "locked_cost_usd": max(0., locked),
               "equity_lower_bound": cash, "net_lower_bound_usd": net,
               "net_without_best_trade_usd": sum(pnls) - max([0.] + pnls),
               "attempts": len(attempts), "proxy_entries": len(entered), "closed_proxy_cycles": len(closed),
               "actual_fills": 0, "unresolved_positions": len(entered) - len(closed),
               "statuses": dict(Counter(a["status"] for a in attempts)), "decision_blocks": dict(blocks),
               "utc_day_pnl_lower_bound": dict(sorted(days.items())), "exposure_day_mean_lower95": daily_lower,
               "observed_clock_span_ms": max(0, observation_end - max(begin_ms, times[0] if times else 0)),
               "continuous_calendar_coverage_established": False,
               "cash_target_reached_in_replay": cash >= 1000, "live_enabled": False,
               "cash_release_assumption_ms": cash_delay_ms, "maker_rebates_credited": 0}
    return {"summary": summary, "attempts": attempts, "cash_ledger": ledger}


def complete_set_diagnostic(decisions, initial_cash):
    """Falsify complete-set quote conditions after fixed receive-time delays.

    Both legs are inspected in the first snapshot at/after each arrival, without
    searching ahead for a better quote. These are observations, never fills.
    """
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("positive initial cash required")
    times = [decision["sample_ms"] for decision in decisions]
    seen, rows = set(), []
    latencies = (1000, 3000)

    def identity(decision):
        raw = decision["raw"]
        return (raw.get("slug"), rules(raw).get("condition_id"),
                raw.get("up_token_id"), raw.get("down_token_id"))

    def leg_check(decision, side, shares, limit, expected, arrival=None):
        raw, rejected = decision["raw"], []
        book = raw.get(side) or {}
        ask, depth = number(book.get("ask")), number(book.get("ask_size"))
        received = number(book.get("received_ms"))
        tick = number(rules(raw).get("tick_size"))
        if identity(decision) != expected:
            rejected.append("market_or_token_identity_changed")
        if book.get("token_id") is not None and book["token_id"] != raw.get(side + "_token_id"):
            rejected.append("book_token_identity_conflict")
        if not quote_valid(raw, side):
            rejected.append("quote_invalid_or_stale")
        if arrival is not None and (received is None or received < arrival):
            rejected.append("quote_received_before_arrival")
        if not (rules(raw).get("active") is True and rules(raw).get("closed") is False
                and rules(raw).get("accepting_orders") is True):
            rejected.append("market_not_open")
        if depth is None or depth < shares:
            rejected.append("insufficient_ask_depth")
        minimum = minimum_shares(decision, ask) if ask is not None else None
        if minimum is None or minimum > shares + 1e-9:
            rejected.append("current_minimum_unmet")
        step = number(decision.get("rules", {}).get("order_size_step"))
        if step is not None and (step <= 0 or abs(shares / step - round(shares / step)) > 1e-7):
            rejected.append("current_size_step_unmet")
        if tick is None or not 0 < tick < 1:
            rejected.append("tick_unknown")
        elif abs(limit / tick - round(limit / tick)) > 1e-7:
            rejected.append("decision_limit_off_current_tick_grid")
        if ask is None or ask > limit + 1e-10:
            rejected.append("decision_limit_exceeded")
        amount = fee(raw, ask, shares) if ask is not None and 0 < ask < 1 else None
        if amount is None:
            rejected.append("fee_unknown")
        return {"side": side, "ask": ask, "ask_size": depth, "received_ms": received,
                "tick_size": tick, "current_minimum_shares": minimum,
                "decision_limit": limit, "fee_usd": amount,
                "quote_constraints_satisfied": not rejected, "reasons": rejected}

    for i, decision in enumerate(decisions):
        if (not decision.get("evaluation_scope_included", True) or not decision_eligible(decision)
                or decision["slug"] in seen):
            continue
        raw = decision["raw"]
        expected = identity(decision)
        if not all(expected) or expected[2] == expected[3]:
            continue
        tick = number(rules(raw).get("tick_size"))
        if tick is None or not 0 < tick < 1 or not all(quote_valid(raw, side) for side in ("up", "down")):
            continue
        minima = [minimum_shares(decision, number(raw[side]["ask"])) for side in ("up", "down")]
        if any(value is None for value in minima):
            continue
        shares = max(minima)
        limits = {side: min(1 - tick, number(raw[side]["ask"]) + tick) for side in ("up", "down")}
        original = {side: leg_check(decision, side, shares, limits[side], expected) for side in ("up", "down")}
        if not all(leg["quote_constraints_satisfied"] for leg in original.values()):
            continue
        total = sum(shares * leg["ask"] + leg["fee_usd"] for leg in original.values())
        if not total < shares or total > initial_cash:
            continue
        seen.add(decision["slug"])
        candidate = {"slug": decision["slug"], "sample_ms": decision["sample_ms"],
                     "shares_per_leg": shares, "quote_cost_usd": total,
                     "gross_redemption_value_usd": shares, "original_quote_legs": original,
                     "executable_profit_established": False, "two_leg_execution_observed": False,
                     "delayed_checks": []}
        rows.append(candidate)  # Retain the original candidate regardless of all later failures.
        for latency in latencies:
            arrival = decision["sample_ms"] + latency
            check = {"latency_ms": latency, "arrival_ms": arrival, "observed_ms": None,
                     "quotes_support_complete_set": False, "quote_cost_usd": None,
                     "actual_fills": 0, "two_leg_execution_observed": False,
                     "cash_receipt_observed": False, "reasons": [], "legs": {},
                     "second_leg_failure_possible": True, "one_leg_only_quote_support": False,
                     "exposure_note": "Non-atomic legs can leave one-sided inventory if only one fills; no fill or inventory is inferred."}
            candidate["delayed_checks"].append(check)
            j = bisect.bisect_left(times, arrival)
            if j >= len(decisions) or times[j] > arrival + 2500:
                check["reasons"].append("arrival_snapshot_unobserved")
                continue
            future = decisions[j]
            check["observed_ms"] = times[j]
            check["observation_lag_ms"] = times[j] - arrival
            if not future.get("evaluation_scope_included", True):
                check["reasons"].append("outside_predefined_sampling_scope")
            if not decision_eligible(future):
                check["reasons"].append("future_snapshot_execution_quality")
            if not continuous(decisions, i, j):
                check["reasons"].append("arrival_path_gap_or_market_change")
            legs = {side: leg_check(future, side, shares, limits[side], expected, arrival)
                    for side in ("up", "down")}
            check["legs"] = legs
            supported = [side for side, leg in legs.items() if leg["quote_constraints_satisfied"]]
            check["one_leg_only_quote_support"] = len(supported) == 1
            if len(supported) != 2:
                check["reasons"].append("one_or_both_leg_constraints_failed")
            receipts = [leg["received_ms"] for leg in legs.values()]
            if None in receipts or abs(receipts[0] - receipts[1]) > 250:
                check["reasons"].append("pair_quote_skew")
            if all(leg["ask"] is not None and leg["fee_usd"] is not None for leg in legs.values()):
                delayed_cost = sum(shares * leg["ask"] + leg["fee_usd"] for leg in legs.values())
                check["quote_cost_usd"] = delayed_cost
                if delayed_cost >= shares:
                    check["reasons"].append("cost_not_below_redemption")
                if delayed_cost > initial_cash:
                    check["reasons"].append("combined_cost_exceeds_cash")
            check["quotes_support_complete_set"] = not check["reasons"]
            stress = {"extra_ticks_per_leg": 1, "quote_cost_usd": None,
                      "quotes_support_complete_set": False, "reasons": list(check["reasons"])}
            check["extra_tick_stress"] = stress
            stressed_cost = 0.
            for side, leg in legs.items():
                price = leg["ask"] + leg["tick_size"] if leg["ask"] is not None and leg["tick_size"] is not None else None
                if price is None or not 0 < price < 1:
                    stress["reasons"].append(side + "_stress_price_invalid")
                    break
                current_min = minimum_shares(future, price)
                amount = fee(future["raw"], price, shares)
                if amount is None or current_min is None or current_min > shares + 1e-9:
                    stress["reasons"].append(side + "_stress_fee_or_minimum_invalid")
                    break
                if price > limits[side] + 1e-10:
                    stress["reasons"].append(side + "_stress_exceeds_decision_limit")
                stressed_cost += shares * price + amount
            else:
                stress["quote_cost_usd"] = stressed_cost
                if stressed_cost >= shares:
                    stress["reasons"].append("stress_cost_not_below_redemption")
                if stressed_cost > initial_cash:
                    stress["reasons"].append("stress_combined_cost_exceeds_cash")
                stress["quotes_support_complete_set"] = not stress["reasons"]
    return {"necessary_quote_conditions": rows, "actual_fills": 0,
            "latencies_ms": list(latencies), "arrival_snapshot_tolerance_ms": 2500,
            "observation_rule": "first_snapshot_at_or_after_arrival; never_search_for_a_better_later_quote",
            "unmodelled": ["non_atomic_second_leg", "actual_order_acceptance_and_fills",
                           "merge_or_redemption_cost_and_cash_delay"]}


def run(input_root, output_root, initial_cash=10., frozen_model=None):
    raw_rows, provenance = load_verified_snapshots(input_root, max_rows=200_000)
    if len(raw_rows) > 200_000:
        raise ValueError("row budget exceeded; stratify whole source archives before loading")
    policy = QualityPolicy()
    dataset = build_quality_dataset(raw_rows, policy, required_features=())
    mark_evaluation_scope(dataset, provenance)
    decisions = dataset["decisions"]
    starts = sorted({d["window_start_ms"] for d in decisions if d["window_start_ms"] is not None})
    if len(starts) < 10:
        raise ValueError("insufficient distinct market windows")
    provenance_hash = digest(provenance)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    result = {"version": VERSION, "research_only": True, "live_enabled": False,
              "source_provenance": provenance, "source_hash": provenance_hash,
              "quality_policy": asdict(policy), "quality_funnel": dataset["funnel"],
              "evaluation_scope": dataset["evaluation_scope"],
              "observed_first_ms": min(d["sample_ms"] for d in decisions),
              "observed_last_ms": max(d["sample_ms"] for d in decisions),
              "independent_market_windows": len(starts), "generated_ms": now_ms,
              "actual_fills": 0, "ten_fill_canary_achieved": False}
    if frozen_model:
        frozen = json.loads(Path(frozen_model).read_text())
        expected = frozen.pop("sha256")
        if digest(frozen) != expected or frozen.get("version") != VERSION:
            raise ValueError("frozen model integrity mismatch")
        if frozen.get("protocol_sha256") != digest(protocol_contract()):
            raise ValueError("frozen model protocol or implementation changed")
        if initial_cash != frozen.get("initial_cash"):
            raise ValueError("frozen initial cash changed")
        begin = frozen["forward_min_window_start_ms"]
        result.update(sample_role="prospective_after_frozen_boundary", frozen_model_sha256=expected,
                      forward_episode_id=expected, cumulative_across_runs=False,
                      evaluation_semantics="replay_of_one_frozen_episode; do_not_add_repeated_runs")
        result["forward"] = replay(decisions, frozen["model"], frozen["threshold"], initial_cash,
                                   begin_ms=begin)
        result["forward_stress"] = replay(decisions, frozen["model"], frozen["threshold"], initial_cash,
                                          latency_ms=3000, extra_exit_ticks=1, begin_ms=begin)
    else:
        train_end = starts[int(len(starts) * .6)]
        validation_end = starts[int(len(starts) * .8)]
        result.update(sample_role="retrospective_discovery_and_untuned_chronological_holdout",
                      split={"train_end_ms": train_end, "validation_end_ms": validation_end,
                             "whole_markets": True, "purge_ms": 60_000},
                      comparisons_registered={"families": FEATURES, "thresholds": THRESHOLDS,
                                              "hold_ms": HOLD_MS, "minimum_validation_cycles": MIN_VALIDATION_CYCLES})
        comparisons = []
        models = {}
        for family in FEATURES:
            examples = training_examples(dataset, train_end, family)
            model = fit_ridge(examples, family, train_end)
            models[family] = model
            for threshold in THRESHOLDS:
                primary = replay(decisions, model, threshold, initial_cash, begin_ms=train_end + 60_000, end_ms=validation_end)
                stress = replay(decisions, model, threshold, initial_cash, latency_ms=3000, extra_exit_ticks=1,
                                begin_ms=train_end + 60_000, end_ms=validation_end)
                p, s = primary["summary"], stress["summary"]
                admissible = (model is not None and p["closed_proxy_cycles"] >= MIN_VALIDATION_CYCLES
                              and p["net_without_best_trade_usd"] > 0 and s["net_without_best_trade_usd"] > 0
                              and p["unresolved_positions"] == s["unresolved_positions"] == 0)
                comparisons.append({"family": family, "threshold": threshold, "model_available": model is not None,
                                    "validation": p, "validation_stress": s, "admissible": admissible})
        viable = [c for c in comparisons if c["admissible"]]
        chosen = max(viable, key=lambda c: c["validation_stress"]["net_without_best_trade_usd"]) if viable else None
        model = models[chosen["family"]] if chosen else None
        threshold = chosen["threshold"] if chosen else THRESHOLDS[0]
        result.update(comparisons=comparisons, selected_family=chosen["family"] if chosen else "no_trade",
                      selection_reason="validation_passed" if chosen else "no_candidate_passed_fixed_validation")
        result["holdout"] = replay(decisions, model, threshold, initial_cash, begin_ms=validation_end + 60_000)
        result["holdout_stress"] = replay(decisions, model, threshold, initial_cash, latency_ms=3000,
                                           extra_exit_ticks=1, begin_ms=validation_end + 60_000)
        # A no-trade model is also frozen; failure is an output, not a reason to tune the holdout.
        frozen = {"version": VERSION, "model": model, "threshold": threshold,
                  "source_hash": provenance_hash, "frozen_at_ms": now_ms,
                  "consumed_through_ms": result["observed_last_ms"],
                  "forward_min_window_start_ms": (max(now_ms, result["observed_last_ms"]) // 300_000 + 1) * 300_000,
                  "initial_cash": initial_cash, "live_enabled": False}
        frozen["protocol_sha256"] = digest(protocol_contract())
        frozen["sha256"] = digest(frozen)
        result["frozen_model_sha256"] = frozen["sha256"]
    result["complete_set_diagnostic"] = complete_set_diagnostic(decisions, initial_cash)
    result["promotion"] = {"canary_eligible": False,
                           "reasons": ["quote_proxies_are_not_private_fills", "prospective_evidence_required",
                                       "execution_and_cash_receipt_reconciliation_required"]}
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "research.json").write_bytes(canonical(result))
    if not frozen_model:
        (output_root / "frozen_model.json").write_bytes(canonical(frozen))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, default=10.)
    parser.add_argument("--frozen-model", type=Path)
    args = parser.parse_args()
    run(args.input, args.output, args.initial_cash, args.frozen_model)
    # No results, parameters, or empirical counts in public Actions stdout.
    print("QUALITY_FIRST_RESEARCH_FINISHED; LIVE_ENABLED=false; RESULTS_REQUIRE_SEALING=true")


if __name__ == "__main__":
    main()
