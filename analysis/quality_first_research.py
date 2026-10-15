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

from quality_first_data import QualityPolicy, build_quality_dataset, load_verified_snapshots

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
        exit_min = minimum_shares(decisions[out], xbook["bid"])
        if exit_min is None or exit_min > shares + 1e-9:
            item["status"] = "exit_minimum_changed"
            continue
        if (number(xbook.get("bid_size")) or 0) < shares:
            item["status"] = "exit_capacity_unobserved"
            continue
        xtick = number(rules(xrow).get("tick_size"))
        if xtick is None or not 0 < xtick < 1:
            item["status"] = "exit_tick_unknown"
            continue
        price = max(0., xbook["bid"] - extra_exit_ticks * xtick)
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
               "utc_day_pnl_lower_bound": dict(sorted(days.items())), "daily_mean_lower95": daily_lower,
               "cash_target_reached_in_replay": cash >= 1000, "live_enabled": False,
               "cash_release_assumption_ms": cash_delay_ms, "maker_rebates_credited": 0}
    return {"summary": summary, "attempts": attempts, "cash_ledger": ledger}


def complete_set_diagnostic(decisions, initial_cash):
    """Necessary quote condition only, never an atomic two-leg fill claim."""
    seen = set()
    rows = []
    for decision in decisions:
        if not decision_eligible(decision) or decision["slug"] in seen:
            continue
        raw = decision["raw"]
        shares = max((minimum_shares(decision, raw[s]["ask"]) or 0) for s in ("up", "down"))
        if shares <= 0:
            continue
        fees = [fee(raw, raw[s]["ask"], shares) for s in ("up", "down")]
        if any(x is None for x in fees):
            continue
        total = shares * (raw["up"]["ask"] + raw["down"]["ask"]) + sum(fees)
        if total < shares and total <= initial_cash and all((number(raw[s].get("ask_size")) or 0) >= shares for s in ("up", "down")):
            seen.add(decision["slug"])
            rows.append({"slug": decision["slug"], "sample_ms": decision["sample_ms"],
                         "quote_cost_usd": total, "gross_redemption_value_usd": shares,
                         "executable_profit_established": False, "two_leg_execution_observed": False})
    return {"necessary_quote_conditions": rows, "actual_fills": 0,
            "unmodelled": ["non_atomic_second_leg", "merge_or_redemption_cost_and_cash_delay"]}


def run(input_root, output_root, initial_cash=10., frozen_model=None):
    raw_rows, provenance = load_verified_snapshots(input_root, max_rows=200_000)
    if len(raw_rows) > 200_000:
        raise ValueError("row budget exceeded; stratify whole source archives before loading")
    policy = QualityPolicy()
    dataset = build_quality_dataset(raw_rows, policy, required_features=())
    decisions = dataset["decisions"]
    starts = sorted({d["window_start_ms"] for d in decisions})
    if len(starts) < 10:
        raise ValueError("insufficient distinct market windows")
    provenance_hash = digest(provenance)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    result = {"version": VERSION, "research_only": True, "live_enabled": False,
              "source_provenance": provenance, "source_hash": provenance_hash,
              "quality_policy": asdict(policy), "quality_funnel": dataset["funnel"],
              "observed_first_ms": min(d["sample_ms"] for d in decisions),
              "observed_last_ms": max(d["sample_ms"] for d in decisions),
              "independent_market_windows": len(starts), "generated_ms": now_ms,
              "actual_fills": 0, "ten_fill_canary_achieved": False}
    if frozen_model:
        frozen = json.loads(Path(frozen_model).read_text())
        expected = frozen.pop("sha256")
        if digest(frozen) != expected or frozen.get("version") != VERSION:
            raise ValueError("frozen model integrity mismatch")
        begin = frozen["forward_min_window_start_ms"]
        result.update(sample_role="prospective_after_frozen_boundary", frozen_model_sha256=expected)
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
