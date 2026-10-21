"""Fail-closed validator for an owner-scoped two-leg maker bundle.

The input is a captured artifact.  This module never connects to Polymarket,
reads a wallet, signs or submits an order, or turns public tape observations
into an authenticated fill.  It checks the evidence needed for ten
chronological *paired* maker entries on one evolving cash ledger.  A
synthetically complete document may pass ``offline_consistency_passed`` only;
the authenticated Canary gate remains false until the receipt origin is
verified outside this offline validator.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from authenticated_compound_reconciler import reconcile_authenticated_chain


SCHEMA = "authenticated-maker-pair-bundle-v1"
REQUIRED_PAIRS = 10
OFFLINE_ONLY = {
    "evidence_scope": "offline_bundle_consistency_only",
    "receipt_authenticity_verified": False,
    "actual_fills": 0,
    "verified_canary_fills": 0,
    "gate_passed": False,
}


def _text(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field}_required")
    return str(value).strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return result


def _timestamp(value: Any, field: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_integer_timestamp") from exc
    return result


def _fail(reason: str, *, pairs: int = 0) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "required_pairs": REQUIRED_PAIRS,
        "pairs": int(pairs),
        "offline_consistency_passed": False,
        **OFFLINE_ONLY,
        "failure_reason": reason,
    }


def _leg(row: dict[str, Any], prefix: str, entry_ts: int) -> tuple[dict[str, Any], str | None]:
    """Validate one leg and return its economic fields."""
    try:
        order_id = _text(row.get(f"{prefix}_order_id"), f"{prefix}_order_id")
        trade_id = _text(row.get(f"{prefix}_trade_id"), f"{prefix}_trade_id")
        tx_ref = _text(row.get(f"{prefix}_transaction_ref"), f"{prefix}_transaction_ref")
        order_type = _text(row.get(f"{prefix}_order_type"), f"{prefix}_order_type").upper()
        order_status = _text(row.get(f"{prefix}_order_status"), f"{prefix}_order_status").upper()
        trade_status = _text(row.get(f"{prefix}_trade_status"), f"{prefix}_trade_status").upper()
        trader_side = _text(row.get(f"{prefix}_trader_side"), f"{prefix}_trader_side").upper()
        acceptance_ts = _timestamp(row.get(f"{prefix}_order_acceptance_ts"), f"{prefix}_order_acceptance_ts")
        matched_ts = _timestamp(row.get(f"{prefix}_trade_matched_ts"), f"{prefix}_trade_matched_ts")
        shares = _decimal(row.get(f"{prefix}_shares"), f"{prefix}_shares")
        price = _decimal(row.get(f"{prefix}_price"), f"{prefix}_price")
        fee_bps = _decimal(row.get(f"{prefix}_fee_rate_bps"), f"{prefix}_fee_rate_bps")
    except ValueError as exc:
        return {}, str(exc)
    if order_type not in {"GTC", "GTD"}:
        return {}, f"{prefix}_order_type_must_be_gtc_or_gtd"
    if row.get(f"{prefix}_post_only") is not True:
        return {}, f"{prefix}_post_only_required"
    if order_status != "MATCHED":
        return {}, f"{prefix}_order_match_status_required"
    if trade_status not in {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}:
        return {}, f"{prefix}_trade_confirmation_required"
    if trader_side != "MAKER":
        return {}, f"{prefix}_trader_side_required"
    if acceptance_ts > entry_ts:
        return {}, f"{prefix}_order_acceptance_after_entry"
    if matched_ts > entry_ts:
        return {}, f"{prefix}_trade_match_after_entry"
    if shares <= 0:
        return {}, f"{prefix}_shares_must_be_positive"
    if price <= 0 or price >= 1:
        return {}, f"{prefix}_price_must_be_between_zero_and_one"
    if fee_bps != Decimal("0"):
        return {}, "maker_fee_not_zero"
    return {
        "order_id": order_id,
        "trade_id": trade_id,
        "transaction_ref": tx_ref,
        "shares": shares,
        "price": price,
        "notional": shares * price,
    }, None


def validate_bundle_document(
    document: Any,
    *,
    required_pairs: int = REQUIRED_PAIRS,
    minimum_order: Any = "5",
) -> dict[str, Any]:
    """Validate one captured paired-maker bundle without authenticating it."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return _fail("invalid_bundle_schema")
    if type(required_pairs) is not int or required_pairs != REQUIRED_PAIRS:
        return _fail("required_pairs_fixed_at_ten")
    try:
        owner = _text(document.get("owner"), "owner")
        ledger_id = _text(document.get("ledger_id"), "ledger_id")
        chain_id = _text(document.get("chain_id"), "chain_id")
        opening = _text(document.get("starting_checkpoint_id"), "starting_checkpoint_id")
        closing = _text(document.get("closing_balance_checkpoint_id"), "closing_balance_checkpoint_id")
        initial_cash = _decimal(document.get("initial_cash", "10"), "initial_cash")
        closing_balance = _decimal(document.get("closing_balance"), "closing_balance")
        floor = _decimal(minimum_order, "minimum_order")
    except ValueError as exc:
        return _fail(str(exc))
    if initial_cash != Decimal("10"):
        return _fail("initial_cash_must_equal_ten")
    if floor <= 0:
        return _fail("minimum_order_must_be_positive")
    if opening == closing:
        return _fail("ledger_anchors_must_differ")
    events = document.get("events")
    if not isinstance(events, list) or len(events) != REQUIRED_PAIRS:
        return _fail("exactly_ten_pair_events_required", pairs=len(events) if isinstance(events, list) else 0)

    seen: dict[str, set[str]] = {key: set() for key in (
        "pair_id", "order_id", "trade_id", "transaction_ref", "merge_receipt_id",
        "settlement_receipt_id", "cash_release_receipt_id", "balance_checkpoint_id",
    )}
    mapped: list[dict[str, Any]] = []
    previous_checkpoint = opening
    previous_entry: int | None = None
    previous_available: int | None = None
    previous_balance_after = initial_cash
    scheduled_releases: list[tuple[int, Decimal, str]] = []
    released_receipts: set[str] = set()

    for index, row in enumerate(events, start=1):
        if not isinstance(row, dict):
            return _fail("invalid_pair_event", pairs=index - 1)
        try:
            if _text(row.get("owner"), "owner") != owner:
                return _fail("owner_scope_mismatch", pairs=index - 1)
            if _text(row.get("ledger_id"), "ledger_id") != ledger_id:
                return _fail("ledger_id_mismatch", pairs=index - 1)
            if _text(row.get("chain_id"), "chain_id") != chain_id:
                return _fail("chain_id_mismatch", pairs=index - 1)
            if row.get("sequence_no") != index:
                return _fail("sequence_no_must_be_exactly_one_to_ten", pairs=index - 1)
            if _text(row.get("status"), "status").lower() != "confirmed":
                return _fail("pair_not_confirmed", pairs=index - 1)
            pair_id = _text(row.get("pair_id"), "pair_id")
            merge_id = _text(row.get("merge_receipt_id"), "merge_receipt_id")
            settlement_id = _text(row.get("settlement_receipt_id"), "settlement_receipt_id")
            cash_id = _text(row.get("cash_release_receipt_id"), "cash_release_receipt_id")
            balance_id = _text(row.get("balance_checkpoint_id"), "balance_checkpoint_id")
            prior_checkpoint = _text(row.get("previous_checkpoint_id"), "previous_checkpoint_id")
            entry_ts = _timestamp(row.get("entry_ts"), "entry_ts")
            available_ts = _timestamp(row.get("cash_available_ts"), "cash_available_ts")
            balance_before = _decimal(row.get("balance_before"), "balance_before")
            balance_after = _decimal(row.get("balance_after"), "balance_after")
            released = _decimal(row.get("released_cash_since_previous_entry"), "released_cash_since_previous_entry")
            entry_cost = _decimal(row.get("entry_cost"), "entry_cost")
            fee = _decimal(row.get("fee"), "fee")
            payout = _decimal(row.get("payout"), "payout")
        except ValueError as exc:
            return _fail(str(exc), pairs=index - 1)
        if prior_checkpoint != previous_checkpoint:
            return _fail("previous_checkpoint_link_mismatch", pairs=index - 1)
        if row.get("cash_reconciled") is not True:
            return _fail("cash_reconciliation_required", pairs=index - 1)
        if row.get("independent_checkpoint") is not True:
            return _fail("independent_balance_checkpoint_required", pairs=index - 1)
        if previous_entry is not None and entry_ts <= previous_entry:
            return _fail("entry_timestamps_must_increase", pairs=index - 1)
        if previous_available is not None and entry_ts <= previous_available:
            return _fail("prior_cash_release_must_precede_next_entry", pairs=index - 1)
        if available_ts <= entry_ts:
            return _fail("cash_available_timestamp_must_follow_entry", pairs=index - 1)
        if index == 1 and released != 0:
            return _fail("first_entry_cannot_use_prior_release", pairs=index - 1)
        if balance_before != previous_balance_after + released:
            return _fail("balance_before_arithmetic_mismatch", pairs=index - 1)
        if balance_after != balance_before - entry_cost - fee:
            return _fail("balance_after_arithmetic_mismatch", pairs=index - 1)
        if any(value < 0 for value in (released, entry_cost, fee, payout)):
            return _fail("negative_ledger_amount", pairs=index - 1)
        if entry_cost < floor:
            return _fail("entry_cost_below_minimum_order", pairs=index - 1)
        if len({merge_id, settlement_id, cash_id}) != 3:
            return _fail("pair_receipts_must_be_independent", pairs=index - 1)

        up, reason = _leg(row, "up", entry_ts)
        if reason:
            return _fail(reason, pairs=index - 1)
        down, reason = _leg(row, "down", entry_ts)
        if reason:
            return _fail(reason, pairs=index - 1)
        expected_cost = up["notional"] + down["notional"]
        if fee != 0 or abs(entry_cost - expected_cost) > Decimal("0.000001"):
            return _fail("pair_entry_cost_arithmetic_mismatch", pairs=index - 1)
        if abs(up["shares"] - down["shares"]) > Decimal("0.000001"):
            return _fail("paired_leg_share_mismatch", pairs=index - 1)
        if payout != up["shares"]:
            return _fail("pair_payout_arithmetic_mismatch", pairs=index - 1)

        # Check all identity values, including both legs, before mapping them
        # to one shared-ledger row.
        all_identity_values = [
            pair_id, merge_id, settlement_id, cash_id, balance_id,
            up["order_id"], down["order_id"], up["trade_id"], down["trade_id"],
            up["transaction_ref"], down["transaction_ref"],
        ]
        if len(set(all_identity_values)) != len(all_identity_values):
            return _fail("duplicate_pair_identity", pairs=index - 1)
        for key, values in (("pair_id", [pair_id]), ("order_id", [up["order_id"], down["order_id"]]),
                            ("trade_id", [up["trade_id"], down["trade_id"]]),
                            ("transaction_ref", [up["transaction_ref"], down["transaction_ref"]]),
                            ("merge_receipt_id", [merge_id]), ("settlement_receipt_id", [settlement_id]),
                            ("cash_release_receipt_id", [cash_id]), ("balance_checkpoint_id", [balance_id])):
            if any(value in seen[key] for value in values):
                return _fail(f"duplicate_{key}", pairs=index - 1)
            seen[key].update(values)

        expected_released = Decimal("0")
        due_receipts: set[str] = set()
        for due_ts, due_amount, due_id in scheduled_releases:
            if due_ts == entry_ts:
                return _fail("cash_available_timestamp_must_precede_next_entry", pairs=index - 1)
            if due_ts < entry_ts and due_id not in released_receipts:
                expected_released += due_amount
                due_receipts.add(due_id)
        if released != expected_released:
            return _fail("released_cash_amount_mismatch", pairs=index - 1)
        released_receipts.update(due_receipts)
        scheduled_releases.append((available_ts, payout, cash_id))
        mapped.append({
            "owner": owner,
            "ledger_id": ledger_id,
            "chain_id": chain_id,
            "sequence_no": index,
            "previous_checkpoint_id": prior_checkpoint,
            "status": "confirmed",
            "trade_id": f"{up['trade_id']}|{down['trade_id']}",
            "transaction_ref": f"{up['transaction_ref']}|{down['transaction_ref']}",
            "settlement_receipt_id": settlement_id,
            "cash_release_receipt_id": cash_id,
            "balance_checkpoint_id": balance_id,
            "cash_reconciled": True,
            "independent_checkpoint": True,
            "entry_ts": entry_ts,
            "cash_available_ts": available_ts,
            "released_cash_since_previous_entry": released,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "entry_cost": entry_cost,
            "fee": fee,
            "payout": payout,
        })
        previous_checkpoint = balance_id
        previous_entry = entry_ts
        previous_available = available_ts
        previous_balance_after = balance_after

    result = reconcile_authenticated_chain(
        mapped,
        owner=owner,
        initial_cash=initial_cash,
        minimum_order=floor,
        required_fills=REQUIRED_PAIRS,
        starting_checkpoint_id=opening,
        closing_balance_checkpoint_id=closing,
        closing_balance=closing_balance,
        require_fee_schedule=False,
    )
    if result.get("failure_reason"):
        return {
            "schema": SCHEMA,
            "required_pairs": REQUIRED_PAIRS,
            **OFFLINE_ONLY,
            "pairs": int(result.get("fills", 0)),
            "offline_consistency_passed": False,
            "failure_reason": result["failure_reason"],
        }
    return {
        "schema": SCHEMA,
        "required_pairs": REQUIRED_PAIRS,
        "pairs": REQUIRED_PAIRS,
        "offline_consistency_passed": True,
        **OFFLINE_ONLY,
        "fills": int(result["fills"]),
        "ending_cash": result["ending_cash"],
        "net_pnl": result["net_pnl"],
        "minimum_cash": result["minimum_cash"],
        "minimum_cash_before_entry": result["minimum_cash_before_entry"],
        "owner_scoped": True,
        "identity_complete": True,
        "unique_pair_ids": len(seen["pair_id"]),
        "unique_order_ids": len(seen["order_id"]),
        "unique_trade_ids": len(seen["trade_id"]),
        "unique_transaction_refs": len(seen["transaction_ref"]),
        "unique_settlement_receipts": len(seen["settlement_receipt_id"]),
        "unique_cash_release_receipts": len(seen["cash_release_receipt_id"]),
        "unique_balance_checkpoints": len(seen["balance_checkpoint_id"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--minimum-order", default="5")
    args = parser.parse_args()
    try:
        document = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = validate_bundle_document(document, minimum_order=args.minimum_order)
    except (OSError, json.JSONDecodeError):
        result = _fail("bundle_read_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("offline_consistency_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

