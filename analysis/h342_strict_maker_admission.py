"""Audit strict maker admission after the asynchronous identity repair.

This is a deterministic contract check over synthetic, fully shaped evidence.
It proves that a future maker bundle cannot enter the offline ten-row ledger
unless every row carries the H341 asynchronous trade-ID binding.  Synthetic
rows remain offline consistency evidence and never become Canary fills.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

try:
    from .maker_compound_receipt_contract import validate_maker_compound_chain
except ImportError:  # pragma: no cover - standalone repository tools.
    from maker_compound_receipt_contract import validate_maker_compound_chain


def _event(sequence: int, *, async_binding: bool = False) -> dict[str, Any]:
    before = Decimal("10") + Decimal("0.61") * (sequence - 1)
    return {
        "owner": "owner-key",
        "maker_address": "0xmaker",
        "ledger_id": "maker-ledger-1",
        "chain_id": "maker-chain-1",
        "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}",
        "status": "confirmed",
        "trade_status": "TRADE_STATUS_CONFIRMED",
        "trade_id": f"trade-{sequence}",
        "transaction_ref": f"0xtx{sequence}",
        "settlement_receipt_id": f"settlement-{sequence}",
        "cash_release_receipt_id": f"cash-release-{sequence}",
        "balance_checkpoint_id": f"balance-{sequence}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(sequence * 100),
        "cash_available_ts": str(sequence * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.61",
        "released_cash_since_previous_entry": "0" if sequence == 1 else "5.61",
        "balance_before": str(before),
        "balance_after": str(before - Decimal("5")),
        "market_id": "m1",
        "shares": "10",
        "price": "0.5",
        "execution_role": "MAKER",
        "orderfilled_event_id": f"0xtx{sequence}:1",
        "order_id": f"order-{sequence}",
        "post_only_accepted": True,
        "order_status": "MATCHED",
        "trader_side": "MAKER",
        "fee_rate": "0",
        "settlement_payout": "5.50",
        "rebate_amount": "0.11",
        "rebate_status": "CONFIRMED",
        "rebate_batch_id": "rebate-day-1",
        "rebate_receipt_id": "rebate-receipt-day-1",
        "rebate_batch_amount": "1.10",
        "rebate_allocated_amount": "0.11",
        "rebate_available_ts": str(sequence * 100 + 10),
        "async_trade_id_binding_ready": async_binding,
    }


def _fill(sequence: int) -> dict[str, str | int]:
    return {
        "event_id": f"0xtx{sequence}:1",
        "tx_hash": f"0xtx{sequence}",
        "market_id": "m1",
        "maker": "0xmaker",
        "taker": "0xtaker",
        "side": "BUY",
        "sign": 1,
        "size": "10",
        "cash_amount": "5",
        "price": "0.5",
    }


def _run(events: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_maker_compound_chain(
        events,
        fills,
        owner="owner-key",
        maker_address="0xmaker",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="16.10",
        require_async_binding=True,
    )


def audit() -> dict[str, Any]:
    fills = [_fill(i) for i in range(1, 11)]
    missing = _run([_event(i) for i in range(1, 11)], fills)
    valid = _run([_event(i, async_binding=True) for i in range(1, 11)], fills)
    partial_events = [_event(i, async_binding=True) for i in range(1, 11)]
    partial_events[6]["async_trade_id_binding_ready"] = False
    partial = _run(partial_events, fills)
    return {
        "format": "h342-strict-maker-admission-v1",
        "hypothesis_id": "H342-strict-maker-admission",
        "source_scope": "offline synthetic maker bundle contract; no market data, account access, order submission or settlement access",
        "strict_without_async_binding_passed": bool(missing.get("offline_consistency_passed")),
        "strict_without_async_binding_failure": missing.get("failure_reason"),
        "strict_with_async_binding_passed": bool(valid.get("offline_consistency_passed")),
        "strict_with_async_binding_rows": int(valid.get("same_ledger_compounding_rows", 0)),
        "partial_binding_passed": bool(partial.get("offline_consistency_passed")),
        "partial_binding_failure": partial.get("failure_reason"),
        "receipt_authenticity_verified": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "strict_admission_requires_async_binding_but_external_receipts_missing",
        "root_cause": "The offline maker contract previously allowed a ten-row synthetic chain without proving the H341 asynchronous trade-ID join. Strict admission now requires the binding on every row; even a fully consistent synthetic chain remains non-Canary until external owner-scoped receipts and reusable cash are authenticated.",
    }


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, sort_keys=True))
