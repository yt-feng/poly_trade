"""Single-position paper ledger with irreversible entry and explicit release.

Candidate construction owns the causal signal and delayed entry snapshot.
This module owns fee-aware sizing, cash reservation, subsequent exit/settlement
evidence and release. A missing profitable exit never erases an entered trade.
There is no fitting, source loading, or trading capability at import time.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable, Mapping


EPSILON = 1e-9


def _timestamp(value: Any) -> datetime | None:
    if value is None or str(value) in {"NaT", "nan", "None"}:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _records(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if hasattr(rows, "to_dict") and hasattr(rows, "columns"):
        return rows.to_dict(orient="records")
    return [dict(row) for row in rows]


def _fee(shares: float, price: float, rate: float) -> float:
    return shares * rate * price * (1.0 - price)


def _entry_terms(
    cash: float, price: float, fraction: float, *, fee_rate: float,
    minimum_notional: float, minimum_shares: float,
) -> dict[str, float] | None:
    """Keep the desired gross fraction, then fit fees without violating floors."""
    # Preserve the inherited sizing rule, including its rounding tolerances.
    if cash < minimum_notional - 1e-12 or not 0.0 < price < 1.0:
        return None
    gross_target = max(minimum_notional, fraction * cash)
    shares = max(minimum_shares, gross_target / price)
    reserved = shares * price + _fee(shares, price, fee_rate)
    if reserved > cash + 1e-12:
        per_share = price + fee_rate * price * (1.0 - price)
        shares = min(shares, cash / per_share)
        reserved = shares * per_share
    cost = shares * price
    fee = _fee(shares, price, fee_rate)
    if shares < minimum_shares - EPSILON or cost < minimum_notional - EPSILON:
        return None
    if reserved > cash + EPSILON:
        return None
    return {"shares": shares, "cost": cost, "entry_fee": fee, "reserved": reserved}


def _resolution_after_entry(
    row: Mapping[str, Any], quote_rows: Iterable[Mapping[str, Any]], *,
    entry: datetime, market_end: datetime, shares: float, reserved: float,
    fee_rate: float, depth_haircut: float, exit_cutoff_seconds: float,
    cash_release_delay_seconds: float,
    exit_latency_seconds: float = 0.0, max_exit_quote_wait_seconds: float = 5.0,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Inspect only after the entry has been committed to the ledger."""
    side = str(row["side"])
    slippage = float(row.get("slippage", 0.0))
    cutoff = market_end - timedelta(seconds=exit_cutoff_seconds)
    supplied_release = _timestamp(row.get("cash_release_ts"))
    # This is an explicit conservative simulation convention, not a receipt.
    release_floor = max(
        market_end + timedelta(seconds=cash_release_delay_seconds),
        supplied_release or market_end,
    )
    ordered_quotes = []
    for quote in quote_rows:
        ts = _timestamp(quote.get("ts_iso", quote.get("ts_utc")))
        if ts is not None and entry < ts < cutoff:
            ordered_quotes.append((ts, quote))
    ordered_quotes.sort(key=lambda item: item[0])
    attempts = 0
    unfilled_attempts = 0

    def executable_terms(quote: Mapping[str, Any]) -> tuple[float, float, float] | None:
        try:
            if bool(quote.get("quote_conflict", False)):
                return None
        except (TypeError, ValueError):
            return None
        bid_cents = _finite(quote.get(f"sell_{side}_cents"))
        size = _finite(quote.get(f"sell_{side}_size"))
        if bid_cents is None or not 0 < bid_cents < 100 or size is None or size * depth_haircut < shares - EPSILON:
            return None
        ask_name = f"buy_{side}_cents"
        if ask_name in quote:
            ask_cents = _finite(quote[ask_name])
            if ask_cents is None or not bid_cents <= ask_cents <= 100:
                return None
        price = bid_cents / 100.0 - slippage
        if not 0.0 < price < 1.0:
            return None
        fee = _fee(shares, price, fee_rate)
        proceeds = shares * price - fee
        if proceeds < reserved - EPSILON:
            return None
        return price, fee, proceeds

    def update_diagnostics() -> None:
        if diagnostics is not None:
            diagnostics.update(exit_order_attempts=attempts, exit_order_unfilled_attempts=unfilled_attempts)

    index = 0
    retry_after = entry
    while index < len(ordered_quotes):
        trigger_ts, quote = ordered_quotes[index]
        terms = executable_terms(quote)
        if trigger_ts <= retry_after or terms is None:
            index += 1
            continue
        attempts += 1
        arrival_ts = trigger_ts
        if exit_latency_seconds > 0:
            target = trigger_ts + timedelta(seconds=exit_latency_seconds)
            deadline = target + timedelta(seconds=max_exit_quote_wait_seconds)
            arrival_index = index + 1
            while arrival_index < len(ordered_quotes) and ordered_quotes[arrival_index][0] < target:
                arrival_index += 1
            if arrival_index == len(ordered_quotes) or ordered_quotes[arrival_index][0] > deadline:
                # The submitted order times out; later quotes can trigger a
                # new attempt only after that timeout. Never choose a later
                # favorable arrival in place of a missing/failed first quote.
                unfilled_attempts += 1
                retry_after = deadline
                index = arrival_index
                continue
            arrival_ts, arrival_quote = ordered_quotes[arrival_index]
            terms = executable_terms(arrival_quote)
            if terms is None:
                unfilled_attempts += 1
                retry_after = arrival_ts
                index = arrival_index + 1
                continue
        price, fee, proceeds = terms
        update_diagnostics()
        return {
            "resolution_kind": "pre_expiry_bid_exit", "resolution_ts": arrival_ts,
            "exit_order_trigger_ts": trigger_ts,
            "release_ts": max(release_floor, arrival_ts), "payout": proceeds,
            "exit_fee": fee, "exit_fill_price": price,
        }

    update_diagnostics()
    outcome = _finite(row.get("outcome_up"))
    available = _timestamp(row.get("label_available_ts"))
    if outcome not in (0.0, 1.0) or available is None:
        return None
    # An alleged pre-expiry binary settlement label is not release evidence.
    if available < market_end:
        return None
    won = (outcome == 1.0) if side == "up" else (outcome == 0.0)
    basis = row.get("label_basis")
    official_bases = {"official_resolution", "official_polymarket_resolution", "authenticated_settlement"}
    is_proxy = basis is not None and str(basis) not in official_bases
    return {
        "resolution_kind": "proxy_label_settlement" if is_proxy else "observed_label_settlement",
        "label_basis": str(basis) if basis is not None else "unspecified",
        "resolution_ts": max(market_end, available),
        "release_ts": max(release_floor, available),
        "payout": shares if won else 0.0,
        "exit_fee": 0.0, "exit_fill_price": None,
    }


def replay_cash_ledger(
    candidates: Any,
    quotes: Mapping[str, Any],
    *,
    initial_cash: float = 10.0,
    base_fraction: float = 0.55,
    boost_fraction: float | None = None,
    boost_after_profits: int = 1,
    fee_rate: float = 0.07,
    minimum_notional: float = 5.0,
    minimum_shares: float = 5.0,
    depth_haircut: float = 1.0,
    exit_cutoff_seconds: float = 30.0,
    exit_latency_seconds: float = 0.0,
    max_exit_quote_wait_seconds: float = 5.0,
    cash_release_delay_seconds: float = 900.0,
    required_fills: int = 10,
    evidence_end_ts: Any = None,
) -> dict[str, Any]:
    """Replay at most ``required_fills`` entries on one evolving cash ledger.

    A known later payout can be calculated after entry, but affects entry
    eligibility and profit-dependent sizing only after its release timestamp.
    Unknown outcomes remain an entered, unresolved position. Optional evidence
    horizon bounds which simulated releases may be counted at terminal time.
    """
    numbers = (initial_cash, base_fraction, fee_rate, minimum_notional,
               minimum_shares, depth_haircut, exit_cutoff_seconds,
               cash_release_delay_seconds, exit_latency_seconds, max_exit_quote_wait_seconds)
    if any(_finite(value) is None for value in numbers):
        raise ValueError("nonfinite ledger configuration")
    if initial_cash <= 0 or minimum_notional <= 0 or minimum_shares <= 0:
        raise ValueError("cash and order floors must be positive")
    if not 0 < base_fraction <= 1 or not 0 < depth_haircut <= 1 or fee_rate < 0:
        raise ValueError("invalid fraction, depth haircut, or fee")
    if boost_fraction is not None and (_finite(boost_fraction) is None or not 0 < boost_fraction <= 1):
        raise ValueError("invalid boost fraction")
    if type(boost_after_profits) is not int or boost_after_profits < 1:
        raise ValueError("invalid boost profit count")
    if type(required_fills) is not int or required_fills < 1:
        raise ValueError("invalid required fill count")
    if min(exit_cutoff_seconds, cash_release_delay_seconds, exit_latency_seconds, max_exit_quote_wait_seconds) < 0:
        raise ValueError("negative clock delay")
    horizon = _timestamp(evidence_end_ts)
    if evidence_end_ts is not None and horizon is None:
        raise ValueError("invalid evidence horizon")

    records = _records(candidates)
    parsed = []
    skips: Counter[str] = Counter()
    for row in records:
        entry = _timestamp(row.get("entry_ts"))
        if entry is None:
            skips["invalid_entry_clock"] += 1
        else:
            parsed.append((entry, str(row.get("slug", "")), row))
    parsed.sort(key=lambda item: (item[0], item[1]))
    cash = float(initial_cash)
    chain: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    consecutive_profits = 0
    released_payout = 0.0
    reserved_total = 0.0
    resolved_count = 0
    last_release: datetime | None = None
    seen: set[str] = set()

    def release_pending() -> None:
        nonlocal cash, pending, consecutive_profits, released_payout, resolved_count, last_release
        assert pending is not None and pending["resolution"] is not None
        resolution = pending["resolution"]
        position = chain[pending["position_index"]]
        payout = float(resolution["payout"])
        cash += payout
        released_payout += payout
        pnl = payout - float(position["reserved"])
        consecutive_profits = consecutive_profits + 1 if pnl > EPSILON else 0
        resolved_count += 1
        last_release = resolution["release_ts"]
        position.update(
            state="released", payout=payout, realized_pnl=pnl,
            resolution_kind=resolution["resolution_kind"],
            resolution_ts=resolution["resolution_ts"].isoformat(),
            cash_reusable_ts=resolution["release_ts"].isoformat(),
            exit_fee=float(resolution["exit_fee"]),
            exit_order_trigger_ts=(resolution["exit_order_trigger_ts"].isoformat()
                                   if resolution.get("exit_order_trigger_ts") else None),
            cash_after_release=cash,
        )
        pending = None

    for entry, slug, row in parsed:
        if len(chain) >= required_fills:
            break
        if horizon is not None and entry > horizon:
            skips["after_evidence_horizon"] += 1
            continue
        if pending is not None:
            resolution = pending["resolution"]
            # Equal timestamps lack event ordering, so require release first.
            if resolution is not None and resolution["release_ts"] < entry:
                release_pending()
            else:
                skips["position_cash_not_released"] += 1
                continue
        if not slug or slug in seen:
            skips["missing_or_duplicate_market"] += 1
            continue
        seen.add(slug)
        market_end = _timestamp(row.get("market_end_ts"))
        decision = _timestamp(row.get("decision_ts"))
        if market_end is None or entry >= market_end or (decision is not None and decision > entry):
            skips["invalid_entry_market_clock"] += 1
            continue
        side = str(row.get("side"))
        price = _finite(row.get("fill_price"))
        depth = _finite(row.get("entry_depth", row.get("depth")))
        slippage = _finite(row.get("slippage", 0.0))
        if side not in {"up", "down"} or price is None or not 0 < price < 1 or slippage is None or slippage < 0:
            skips["invalid_entry_terms"] += 1
            continue
        fraction = (boost_fraction if boost_fraction is not None and consecutive_profits >= boost_after_profits
                    else base_fraction)
        terms = _entry_terms(cash, price, fraction, fee_rate=fee_rate,
                             minimum_notional=minimum_notional, minimum_shares=minimum_shares)
        if terms is None:
            skips["minimum_order_not_affordable"] += 1
            continue
        if depth is None or depth * depth_haircut < terms["shares"] - EPSILON:
            skips["insufficient_observed_entry_depth"] += 1
            continue

        # Commit the entry before any future quote or terminal label is read.
        before = cash
        cash -= terms["reserved"]
        reserved_total += terms["reserved"]
        chain.append({
            "slug": slug, "run_name": str(row.get("run_name", "")),
            "entry_ts": entry.isoformat(), "market_end_ts": market_end.isoformat(),
            "side": side, "fraction": float(fraction), "fill_price": price,
            "released_consecutive_profits_before_entry": consecutive_profits,
            "cash_before_entry": before, "cash_after_entry": cash,
            **terms, "state": "entered_unresolved", "payout": None,
            "realized_pnl": None, "cash_reusable_ts": None,
            "label_basis": str(row.get("label_basis", "unspecified")),
        })
        resolution = _resolution_after_entry(
            row, _records(quotes.get(slug)), entry=entry, market_end=market_end,
            shares=terms["shares"], reserved=terms["reserved"], fee_rate=fee_rate,
            depth_haircut=depth_haircut, exit_cutoff_seconds=exit_cutoff_seconds,
            cash_release_delay_seconds=cash_release_delay_seconds,
            exit_latency_seconds=exit_latency_seconds,
            max_exit_quote_wait_seconds=max_exit_quote_wait_seconds,
            diagnostics=chain[-1],
        )
        pending = {"position_index": len(chain) - 1, "resolution": resolution}

    if pending is not None and pending["resolution"] is not None:
        release_at = pending["resolution"]["release_ts"]
        if horizon is None or release_at <= horizon:
            release_pending()

    entered_count = len(chain)
    unresolved_count = entered_count - resolved_count
    positive = cash > float(initial_cash) + EPSILON
    passed = entered_count == required_fills and resolved_count == required_fills and positive
    if unresolved_count:
        failure = "entered_position_not_released"
    elif entered_count < required_fills:
        failure = "minimum_order_not_affordable" if skips["minimum_order_not_affordable"] else "fewer_than_required_entries"
    elif not positive:
        failure = "nonpositive_net_cash_growth"
    else:
        failure = None
    first_entry = _timestamp(chain[0]["entry_ts"]) if chain else None
    locked_cost = sum(float(item["reserved"]) for item in chain if item["state"] != "released")
    return {
        "format": "causal-single-cash-ledger-v1", "candidate_rows": len(records),
        "initial_cash": float(initial_cash), "ending_cash": cash,
        "entries": entered_count, "fills": entered_count,
        "resolved_fills": resolved_count, "unresolved_fills": unresolved_count,
        "required_fills": required_fills, "first_ten_fills": entered_count,
        "first_ten_ending_cash": cash, "first_ten_passed": passed,
        "positive_net_growth": positive, "net_cash_change": cash - float(initial_cash),
        "first_ten_min_cash_before_entry": min((float(item["cash_before_entry"]) for item in chain), default=float(initial_cash)),
        "entry_outlay_total": reserved_total, "released_payout_total": released_payout,
        "realized_pnl": sum(float(item["realized_pnl"]) for item in chain if item["state"] == "released"),
        "locked_position_cost": locked_cost,
        "cash_conservation_error": cash - (float(initial_cash) - reserved_total + released_payout),
        "simulated_cash_reusable_ts": last_release.isoformat() if last_release else None,
        "elapsed_seconds": (last_release - first_entry).total_seconds() if last_release and first_entry else None,
        "failure_reason": failure, "skipped": dict(skips), "chain": chain,
        "cash_release_is_simulated_assumption": True,
        "exit_latency_seconds": float(exit_latency_seconds),
        "max_exit_quote_wait_seconds": float(max_exit_quote_wait_seconds),
        "exit_order_attempts": sum(item["exit_order_attempts"] for item in chain),
        "exit_order_unfilled_attempts": sum(item["exit_order_unfilled_attempts"] for item in chain),
        "same_ledger": True, "actual_fills": 0, "verified_canary_fills": 0,
        "live_orders_enabled": False,
    }
