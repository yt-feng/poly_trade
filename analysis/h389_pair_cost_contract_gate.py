"""Apply the current per-order contract to an online public-wallet pair replay.

H388 scaled a matched pair to a $5 *total* budget.  The current Polymarket
contract describes the minimum order size as $5 USDC notional per order, so a
two-leg pair must be rebuilt with a per-leg floor before it can be considered.
This module processes the already-pinned public activity tape in timestamp
order.  It is an observational upper bound only: public wallet prints are not
our account fills and never advance the authenticated Canary count.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


INITIAL_CASH = Decimal("10")
MIN_ORDER_NOTIONAL = Decimal("5")
MIN_ORDER_SHARES = Decimal("5")
TAKER_RATE = Decimal("0.07")


def dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid_decimal") from exc
    if not result.is_finite():
        raise ValueError("non_finite_decimal")
    return result


def _outcome(event: dict[str, Any]) -> str | None:
    value = str(event.get("outcome") or "").strip().lower()
    if value in {"up", "down"}:
        return value
    index = event.get("outcomeIndex")
    if index == 0:
        return "up"
    if index == 1:
        return "down"
    return None


def _fee(size: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    if size <= 0 or price <= 0 or price >= 1 or rate <= 0:
        return Decimal("0")
    return size * rate * price * (Decimal("1") - price)


def pair_contract_budget(up_price: Decimal, down_price: Decimal) -> dict[str, Decimal]:
    """Return the minimum matched pair size and cash under per-leg floors.

    A common matched size must satisfy both the five-share floor and the $5
    notional floor on each leg.  The same size is used for both legs so the
    pair can be redeemed without treating unmatched inventory as cash.
    """
    up_price = dec(up_price)
    down_price = dec(down_price)
    if up_price <= 0 or down_price <= 0 or up_price >= 1 or down_price >= 1:
        raise ValueError("prices_must_be_between_zero_and_one")
    matched_shares = max(
        MIN_ORDER_SHARES,
        MIN_ORDER_NOTIONAL / up_price,
        MIN_ORDER_NOTIONAL / down_price,
    )
    up_cost = matched_shares * up_price
    down_cost = matched_shares * down_price
    return {
        "matched_shares": matched_shares,
        "up_cost": up_cost,
        "down_cost": down_cost,
        "pair_cost_maker": up_cost + down_cost,
        "pair_fee_taker": _fee(matched_shares, up_price, TAKER_RATE)
        + _fee(matched_shares, down_price, TAKER_RATE),
    }


def extract_online_pairs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create one causal candidate per slug once both legs meet the contract."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if isinstance(event, dict) and str(event.get("slug") or ""):
            grouped[str(event["slug"])].append(event)
    rows: list[dict[str, Any]] = []
    for slug, market_events in grouped.items():
        ordered = sorted(
            market_events,
            key=lambda event: (
                int(event.get("timestamp") or 0),
                str(event.get("transactionHash") or ""),
            ),
        )
        up_shares = down_shares = Decimal("0")
        up_cost = down_cost = Decimal("0")
        candidate: dict[str, Any] | None = None
        redeem_ts: list[int] = []
        for event in ordered:
            timestamp = int(event.get("timestamp") or 0)
            kind = str(event.get("type") or "").upper()
            if kind == "REDEEM":
                redeem_ts.append(timestamp)
                continue
            if kind != "TRADE" or str(event.get("side") or "").upper() != "BUY":
                continue
            outcome = _outcome(event)
            if outcome not in {"up", "down"}:
                continue
            size = dec(event.get("size", 0))
            cost = dec(event.get("usdcSize", 0))
            price = dec(event.get("price", 0))
            if size <= 0 or cost < 0 or price <= 0 or price >= 1:
                continue
            if outcome == "up":
                up_shares += size
                up_cost += cost
            else:
                down_shares += size
                down_cost += cost
            if candidate is not None or up_shares <= 0 or down_shares <= 0:
                continue
            up_price = up_cost / up_shares
            down_price = down_cost / down_shares
            budget = pair_contract_budget(up_price, down_price)
            if up_shares < budget["matched_shares"] or down_shares < budget["matched_shares"]:
                continue
            candidate = {
                "slug": slug,
                "entry_ts": timestamp,
                "up_price": up_price,
                "down_price": down_price,
                "up_shares_observed": up_shares,
                "down_shares_observed": down_shares,
                **budget,
            }
        if candidate is None or not redeem_ts:
            continue
        candidate["release_ts"] = max(redeem_ts)
        if candidate["release_ts"] < candidate["entry_ts"]:
            continue
        rows.append({key: str(value) if isinstance(value, Decimal) else value for key, value in candidate.items()})
    return sorted(rows, key=lambda row: (int(row["entry_ts"]), row["slug"]))


def replay(rows: list[dict[str, Any]], *, fee_mode: str) -> dict[str, Any]:
    if fee_mode not in {"maker", "taker"}:
        raise ValueError("unsupported_fee_mode")
    cash = INITIAL_CASH
    pending: list[tuple[int, Decimal]] = []
    accepted: list[dict[str, Any]] = []
    skipped_contract = 0
    skipped_cash = 0
    for row in rows:
        entry_ts = int(row["entry_ts"])
        pending = sorted(pending)
        released: list[tuple[int, Decimal]] = []
        while pending and pending[0][0] <= entry_ts:
            released.append(pending.pop(0))
        cash += sum((value for _, value in released), Decimal("0"))
        pair_cost = dec(row["pair_cost_maker"])
        fee = dec(row["pair_fee_taker"]) if fee_mode == "taker" else Decimal("0")
        required_cash = pair_cost + fee
        if required_cash > cash:
            skipped_cash += 1
            continue
        cash_before = cash
        cash -= required_cash
        # A matched pair pays one USDC per matched share at redemption.  This
        # is an observational settlement assumption, not an authenticated
        # account receipt.
        payout = dec(row["matched_shares"])
        pending.append((int(row["release_ts"]), payout))
        accepted.append(
            {
                "slug": row["slug"],
                "entry_ts": entry_ts,
                "release_ts": int(row["release_ts"]),
                "cash_before": str(cash_before),
                "required_cash": str(required_cash),
                "payout": str(payout),
                "cash_after_entry": str(cash),
            }
        )
    for _, value in pending:
        cash += value
    return {
        "fee_mode": fee_mode,
        "input_rows": len(rows),
        "accepted_rows": len(accepted),
        "skipped_cash": skipped_cash,
        "skipped_contract": skipped_contract,
        "first_ten_rows": len(accepted[:10]),
        "first_ten_min_cash_before_entry": str(
            min((dec(row["cash_before"]) for row in accepted[:10]), default=Decimal("0"))
        ),
        "ending_cash_after_release": str(cash),
        "same_ledger_no_reset": True,
        "concatenation_count": 0,
        "ten_row_observational_gate": len(accepted[:10]) == 10,
        "rows": accepted,
    }


def build_report(events: list[dict[str, Any]], *, source_commit: str) -> dict[str, Any]:
    rows = extract_online_pairs(events)
    return {
        "format": "h389-pair-cost-per-order-contract-gate-v1",
        "source": {
            "repository": "JREREEE/btc-5m-market-trading-bot",
            "commit": source_commit,
            "activity_file": "results/target/target_activity.json",
            "observational_wallet_tape": True,
        },
        "paper_only": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "contract": {
            "initial_cash": str(INITIAL_CASH),
            "minimum_order_notional_per_leg": str(MIN_ORDER_NOTIONAL),
            "minimum_order_shares_per_leg": str(MIN_ORDER_SHARES),
            "taker_fee_rate": str(TAKER_RATE),
            "maker_fee_rate": "0",
            "official_source": "https://docs.polymarket.com/market-data/market-details#trading-constraints",
            "official_fee_source": "https://docs.polymarket.com/trading/fees",
        },
        "causal_observation": {
            "timestamp_ordered": True,
            "entry_requires_both_legs_observed": True,
            "future_leg_leakage": False,
            "release_requires_observed_redeem": True,
        },
        "maker_replay": replay(rows, fee_mode="maker"),
        "taker_replay": replay(rows, fee_mode="taker"),
        "rows": rows,
        "decision": "contract_gate_only; pair_total_budget_from_h388_is_invalid_for_per_order_floor; do_not_promote_or_submit_orders",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    events = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(events, list):
        raise SystemExit("input_must_be_list")
    report = build_report(events, source_commit=args.source_commit)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
