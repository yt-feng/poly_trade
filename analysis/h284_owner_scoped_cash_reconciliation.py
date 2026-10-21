"""Reconcile observed activity with owner-scoped collateral cash movement.

This module is deliberately offline.  A private runner supplies normalized
activity rows and already fetched transaction-log rows; this code never
connects to a wallet, signs, submits, or reads an account balance.  A
``PayoutRedemption`` event proves a protocol payout action, while the
owner-directed collateral transfer proves that cash reached the wallet.  The
two are kept separate from the ten-fill Canary gate.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable


MICRO = Decimal("1000000")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _micro(value: Any, field: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field}_must_be_nonnegative_decimal")
    scaled = amount * MICRO
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field}_must_have_at_most_six_decimals")
    return int(scaled)


def _amount(value: Any, field: str) -> Decimal:
    return Decimal(_micro(value, field)) / MICRO


def _normalize_activity(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("activity_row_must_be_object")
        kind = _text(row.get("type")).upper()
        if kind not in {"TRADE", "REDEEM"}:
            raise ValueError("unsupported_activity_type")
        side = _text(row.get("side")).upper()
        if kind == "TRADE" and side not in {"BUY", "SELL"}:
            raise ValueError("trade_side_required")
        if kind == "REDEEM":
            side = ""
        tx = _text(row.get("transaction_ref"))
        if not tx:
            raise ValueError("transaction_ref_required")
        timestamp = row.get("timestamp")
        try:
            timestamp = int(str(timestamp))
        except (TypeError, ValueError) as exc:
            raise ValueError("timestamp_required") from exc
        amount = _micro(row.get("usdc_size"), "usdc_size")
        delta = amount if kind == "REDEEM" or side == "SELL" else -amount
        normalized.append({
            "type": kind,
            "side": side,
            "transaction_ref": tx,
            "timestamp": timestamp,
            "amount_micro": amount,
            "delta_micro": delta,
        })
    return sorted(normalized, key=lambda row: (row["timestamp"], row["transaction_ref"], row["type"]))


def reconcile_observed_cash(
    activity_rows: Iterable[dict[str, Any]],
    transfer_rows: Iterable[dict[str, Any]],
    *,
    owner: str,
    collateral_token: str,
    initial_cash: Any = "10",
    payout_receipts: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Join public activity to collateral transfers and build one cash path.

    ``transfer_rows`` are already decoded ERC-20 ``Transfer`` logs with
    ``transaction_ref``, ``token``, ``from``, ``to`` and integer
    ``amount_base_units`` fields.  ``payout_receipts`` contains decoded
    ``PayoutRedemption`` rows with ``transaction_ref`` and
    ``amount_base_units``.  Amounts are compared in base units, so a rounded
    API display cannot create a false join.
    """
    expected_owner = _text(owner).lower()
    expected_token = _text(collateral_token).lower()
    if not expected_owner or not expected_token:
        raise ValueError("owner_and_collateral_token_required")
    activities = _normalize_activity(activity_rows)
    if not activities:
        raise ValueError("activity_rows_required")
    expected: dict[str, int] = defaultdict(int)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in activities:
        tx = row["transaction_ref"]
        expected[tx] += row["delta_micro"]
        grouped[tx].append(row)

    actual: dict[str, int] = defaultdict(int)
    inbound: dict[str, int] = defaultdict(int)
    outbound: dict[str, int] = defaultdict(int)
    for row in transfer_rows:
        if not isinstance(row, dict):
            raise ValueError("transfer_row_must_be_object")
        if _text(row.get("token")).lower() != expected_token:
            continue
        tx = _text(row.get("transaction_ref"))
        if not tx:
            raise ValueError("transfer_transaction_ref_required")
        try:
            amount = int(row.get("amount_base_units"))
        except (TypeError, ValueError) as exc:
            raise ValueError("transfer_amount_must_be_integer") from exc
        if amount < 0:
            raise ValueError("transfer_amount_must_be_nonnegative")
        source = _text(row.get("from")).lower()
        target = _text(row.get("to")).lower()
        if target == expected_owner:
            actual[tx] += amount
            inbound[tx] += amount
        if source == expected_owner:
            actual[tx] -= amount
            outbound[tx] += amount

    payout_by_tx: dict[str, int] = defaultdict(int)
    payout_rows = 0
    for row in payout_receipts:
        if not isinstance(row, dict):
            raise ValueError("payout_receipt_must_be_object")
        tx = _text(row.get("transaction_ref"))
        if not tx:
            raise ValueError("payout_transaction_ref_required")
        try:
            amount = int(row.get("amount_base_units"))
        except (TypeError, ValueError) as exc:
            raise ValueError("payout_amount_must_be_integer") from exc
        if amount < 0:
            raise ValueError("payout_amount_must_be_nonnegative")
        payout_by_tx[tx] += amount
        payout_rows += 1

    joined = [tx for tx, amount in expected.items() if actual.get(tx, 0) == amount]
    trade_txs = {row["transaction_ref"] for row in activities if row["type"] == "TRADE"}
    redeem_txs = {row["transaction_ref"] for row in activities if row["type"] == "REDEEM"}
    redeem_payout_matches = [
        tx for tx in redeem_txs
        if payout_by_tx.get(tx, 0) == inbound.get(tx, 0) == expected.get(tx, 0)
    ]

    opening = _amount(initial_cash, "initial_cash")
    cash = opening
    minimum = opening
    first_negative: int | None = None
    for index, row in enumerate(activities, 1):
        cash += Decimal(row["delta_micro"]) / MICRO
        minimum = min(minimum, cash)
        if cash < 0 and first_negative is None:
            first_negative = index
    required_initial = max(Decimal("0"), opening - minimum)
    exact_all = len(joined) == len(expected)
    payout_transactions = set(payout_by_tx)
    return {
        "activity_rows": len(activities),
        "unique_activity_transactions": len(expected),
        "trade_activity_rows": sum(row["type"] == "TRADE" for row in activities),
        "redeem_activity_rows": sum(row["type"] == "REDEEM" for row in activities),
        "exact_collateral_delta_join_transactions": len(joined),
        "exact_trade_collateral_delta_join_transactions": len(trade_txs & set(joined)),
        "exact_redeem_collateral_delta_join_transactions": len(redeem_txs & set(joined)),
        "payout_receipt_rows": payout_rows,
        "payout_redeem_transaction_matches": len(redeem_payout_matches),
        "payout_transaction_count": len(payout_transactions),
        "payout_amount_inbound_match_transactions": sum(
            payout_by_tx.get(tx, 0) == inbound.get(tx, 0)
            for tx in payout_transactions
        ),
        "payout_amount_activity_match_transactions": sum(
            payout_by_tx.get(tx, 0) == expected.get(tx, 0)
            for tx in payout_transactions
        ),
        "cash_release_transaction_rows": sum(tx in inbound for tx in redeem_txs),
        "minimum_cash": str(minimum),
        "required_initial_cash": str(required_initial),
        "first_negative_event_index": first_negative,
        "ending_cash": str(cash),
        "historical_cash_reuse_evidence": bool(redeem_payout_matches),
        "same_ledger_10_canary_eligible": (
            len(activities) == 10
            and all(row["type"] == "TRADE" for row in activities)
            and exact_all and first_negative is None
            and len(redeem_payout_matches) == len(redeem_txs)
        ),
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }
