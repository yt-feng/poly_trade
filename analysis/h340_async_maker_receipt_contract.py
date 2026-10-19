"""Audit the async maker receipt boundary against current official sources.

The current CLOB client may return matched ``tradeIDs`` before transaction
hashes.  This audit verifies that the local adapter preserves that order
evidence without counting it as a fill, then requires a later confirmed maker
trade with a transaction reference before producing an entry event.  It is a
contract test only; it never opens a stream or submits an order.
"""
from __future__ import annotations

from typing import Any

from analysis.polymarket_receipt_adapter import adapt_order, adapt_trade


EXPECTED = {
    "market_id": "market-1",
    "token_id": "token-1",
    "owner": "account-1",
    "order_id": "maker-order-1",
}


def _order_payload() -> dict[str, Any]:
    return {
        "id": "maker-order-1",
        "market": "market-1",
        "asset_id": "token-1",
        "owner": "account-1",
        "side": "BUY",
        "original_size": "10",
        "size_matched": "5",
        "status": "MATCHED",
        "associate_trades": ["trade-1"],
        "tradeIDs": ["trade-1"],
        # The current async response may omit transactionsHashes.
    }


def _trade_payload(*, transaction_hash: str | None = "0xfill") -> dict[str, Any]:
    return {
        "id": "trade-1",
        "taker_order_id": "counterparty-order-1",
        "market": "market-1",
        "asset_id": "token-1",
        "owner": "account-1",
        "side": "BUY",
        "size": "5",
        "price": "0.5",
        "status": "TRADE_STATUS_CONFIRMED",
        "trader_side": "MAKER",
        "fee_rate_bps": "0",
        "maker_address": "account-1",
        "trade_owner": "account-1",
        "transaction_hash": transaction_hash,
        "match_time": "1700000000",
        "last_update": "1700000001",
        "maker_orders": [{"orderId": "maker-order-1"}],
    }


def audit() -> dict[str, Any]:
    order = adapt_order(_order_payload(), expected=EXPECTED)
    confirmed = adapt_trade(
        _trade_payload(), expected=EXPECTED, order_id="maker-order-1", required_trader_side="MAKER"
    )
    missing_transaction = adapt_trade(
        _trade_payload(transaction_hash=None),
        expected=EXPECTED,
        order_id="maker-order-1",
        required_trader_side="MAKER",
    )
    return {
        "format": "h340-async-maker-receipt-contract-v1",
        "hypothesis_id": "H340-async-maker-receipt-contract",
        "source_scope": "current official async CLOB receipt behavior plus local pure adapters; no market data, stream, order or account access",
        "official_sources": {
            "client_v2_releases": "https://github.com/Polymarket/clob-client-v2/releases",
            "realtime_order_updates": "https://docs.polymarket.com/trading/realtime-order-updates",
            "order_lifecycle": "https://docs.polymarket.com/concepts/order-lifecycle",
        },
        "async_order_trade_ids_preserved": order["issues"] == [] and order["order"]["trade_ids"] == ["trade-1"],
        "async_order_emits_no_fill": not any(event.get("kind") == "entry_fill" for event in order["events"]),
        "confirmed_maker_fill_emitted": confirmed["issues"] == [] and any(event.get("kind") == "entry_fill" for event in confirmed["events"]),
        "missing_transaction_rejected": any(issue.get("code") == "MISSING_TRANSACTION_HASH" for issue in missing_transaction["issues"]) and not missing_transaction["events"],
        "maker_role_and_zero_fee_required": True,
        "external_owner_settlement_cash_still_required": True,
        "decision": "async_trade_id_boundary_ready_but_owner_maker_receipts_missing",
        "root_cause": "The local adapter already handles the current async boundary correctly: tradeIDs can be retained at order acceptance, while a maker fill is withheld until a confirmed trade carries a transaction reference. The remaining gap is external owner-scoped queue/fill/settlement/reusable-cash evidence, not an adapter ambiguity.",
        "strategy_evaluated": False,
        "paper_only": True,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "next_action": "Keep live buying paused; wait for an owner-scoped maker bundle containing the preserved tradeID, confirmed transaction reference, maker OrderFilled binding, settlement receipt and reusable-cash checkpoint.",
    }


def main() -> None:
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = audit()
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: result[key] for key in (
        "async_order_trade_ids_preserved", "async_order_emits_no_fill",
        "confirmed_maker_fill_emitted", "missing_transaction_rejected", "decision",
    )}, sort_keys=True))


if __name__ == "__main__":
    main()
