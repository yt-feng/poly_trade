"""Audit a public wallet tape for a scaled pair-cost cash-chain upper bound.

The input is an observational public activity export, not our account's
execution record.  A market is eligible only when both legs have been bought,
the source has a redeem event, and the matched portion can be scaled to a
single $5 pair.  The scaled rows are then replayed through one $10 ledger with
no reset or concatenation.  This is an upper bound and never a Canary.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


INITIAL_CASH = Decimal("10")
SPLIT_SIZE = Decimal("5")
MIN_SHARES = Decimal("5")
TAKER_RATE = Decimal("0.072")
REDEEM_DELAY_FALLBACK = 366


def dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid_decimal") from exc
    if not result.is_finite():
        raise ValueError("non_finite_decimal")
    return result


def _market_start(slug: str) -> int | None:
    try:
        return int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


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


def _fee_for_trade(size: Decimal, price: Decimal) -> Decimal:
    if size <= 0 or price <= 0 or price >= 1:
        return Decimal("0")
    return size * TAKER_RATE * price * (Decimal("1") - price)


def extract_markets(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "start": None,
        "up_shares": Decimal("0"),
        "down_shares": Decimal("0"),
        "up_cost": Decimal("0"),
        "down_cost": Decimal("0"),
        "up_fee": Decimal("0"),
        "down_fee": Decimal("0"),
        "redeem_ts": [],
        "redeem_usdc": Decimal("0"),
        "trade_rows": 0,
    })
    for event in events:
        if not isinstance(event, dict):
            continue
        slug = str(event.get("slug") or "")
        if not slug:
            continue
        row = grouped[slug]
        row["start"] = _market_start(slug)
        kind = str(event.get("type") or "").upper()
        if kind == "REDEEM":
            row["redeem_ts"].append(int(event.get("timestamp") or 0))
            row["redeem_usdc"] += dec(event.get("usdcSize", 0))
            continue
        if kind != "TRADE" or str(event.get("side") or "").upper() != "BUY":
            continue
        outcome = _outcome(event)
        if outcome not in {"up", "down"}:
            continue
        size = dec(event.get("size", 0))
        cost = dec(event.get("usdcSize", 0))
        price = dec(event.get("price", 0))
        if size <= 0 or cost < 0:
            continue
        row[f"{outcome}_shares"] += size
        row[f"{outcome}_cost"] += cost
        row[f"{outcome}_fee"] += _fee_for_trade(size, price)
        row["trade_rows"] += 1
    result = []
    for slug, row in grouped.items():
        start = row["start"]
        up, down = row["up_shares"], row["down_shares"]
        matched = min(up, down)
        if start is None or matched <= 0 or not row["redeem_ts"]:
            continue
        avg_up = row["up_cost"] / up if up > 0 else Decimal("0")
        avg_down = row["down_cost"] / down if down > 0 else Decimal("0")
        matched_cost = matched * (avg_up + avg_down)
        if matched_cost <= 0:
            continue
        # Allocate each side's observed fee in proportion to the matched leg.
        matched_fee = (row["up_fee"] / up * matched) + (row["down_fee"] / down * matched)
        scale = SPLIT_SIZE / matched_cost
        scaled_shares = matched * scale
        if scaled_shares < MIN_SHARES:
            continue
        pair_cost = matched_cost / matched
        result.append({
            "slug": slug,
            "start": start,
            "release_ts": max(row["redeem_ts"]),
            "trade_rows": row["trade_rows"],
            "matched_shares_observed": str(matched),
            "unmatched_shares_observed": str(abs(up - down)),
            "observed_matched_cost": str(matched_cost),
            "observed_pair_cost": str(pair_cost),
            "scaled_shares": str(scaled_shares),
            "scaled_pair_cost": str(pair_cost),
            "scaled_pnl_zero_fee": str(SPLIT_SIZE * (Decimal("1") / pair_cost - Decimal("1"))),
            "scaled_fee_stress": str(matched_fee * scale),
            "scaled_pnl_taker_stress": str(SPLIT_SIZE * (Decimal("1") / pair_cost - Decimal("1")) - matched_fee * scale),
            "redeem_usdc_observed": str(row["redeem_usdc"]),
        })
    return sorted(result, key=lambda row: (row["start"], row["slug"]))


def replay(rows: list[dict[str, Any]], pnl_key: str) -> dict[str, Any]:
    cash = INITIAL_CASH
    pending: list[tuple[int, Decimal]] = []
    ledger: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        start = int(row["start"])
        released = Decimal("0")
        still = []
        for release_ts, value in pending:
            if release_ts <= start:
                cash += value
                released += value
            else:
                still.append((release_ts, value))
        pending = still
        if cash < SPLIT_SIZE:
            skipped += 1
            ledger.append({"start": start, "slug": row["slug"], "eligible": False, "cash_before": str(cash), "released": str(released), "reason": "insufficient_free_cash"})
            continue
        pnl = dec(row[pnl_key])
        cash_before = cash
        cash -= SPLIT_SIZE
        pending.append((max(int(row["release_ts"]), start + 300), SPLIT_SIZE + pnl))
        ledger.append({"start": start, "slug": row["slug"], "eligible": True, "cash_before": str(cash_before), "released": str(released), "pnl": str(pnl), "return_at_release": str(SPLIT_SIZE + pnl), "release_ts": max(int(row["release_ts"]), start + 300)})
    for _, value in sorted(pending):
        cash += value
    eligible = [row for row in ledger if row["eligible"]]
    first_ten = eligible[:10]
    min_cash = min((dec(row["cash_before"]) for row in first_ten), default=Decimal("0"))
    return {
        "pnl_key": pnl_key,
        "input_markets": len(rows),
        "same_ledger_rows": len(eligible),
        "skipped_insufficient_cash": skipped,
        "first_ten_rows": len(first_ten),
        "first_ten_min_cash_before_entry": str(min_cash),
        "ending_cash_after_release": str(cash),
        "ten_row_same_ledger_gate": len(first_ten) == 10 and min_cash >= SPLIT_SIZE,
        "growth_positive": cash > INITIAL_CASH,
        "ledger_reset_count": 0,
        "concatenation_count": 0,
        "ledger": ledger,
    }


def build_report(events: list[dict[str, Any]], *, source_commit: str) -> dict[str, Any]:
    rows = extract_markets(events)
    return {
        "format": "h388-pair-cost-public-wallet-audit-v1",
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
        "public_wallet_rows": len(events),
        "eligible_pair_markets": len(rows),
        "cash_ledger_protocol": {
            "initial_cash": str(INITIAL_CASH),
            "fixed_pair_budget": str(SPLIT_SIZE),
            "minimum_shares": str(MIN_SHARES),
            "no_reset": True,
            "no_concatenation": True,
            "release_requires_observed_redeem": True,
            "observed_wallet_is_not_our_fill": True,
        },
        "replay_zero_fee_upper_bound": replay(rows, "scaled_pnl_zero_fee"),
        "replay_taker_fee_stress": replay(rows, "scaled_pnl_taker_stress"),
        "rows": rows,
        "decision": "observational_upper_bound_only; do_not_promote_or_submit_orders",
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
