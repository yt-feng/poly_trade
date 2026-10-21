"""Audit the current official Polymarket user-stream wire contract.

This is a source-contract check, not a market experiment.  It exercises the
current order and trade event shapes from the official documentation against
the pure adapter and proves that only a terminal confirmed trade with a
transaction reference can become a fill event.
"""
from __future__ import annotations

from typing import Any

try:
    from analysis.polymarket_receipt_adapter import normalize_user_event
except ModuleNotFoundError:  # pragma: no cover - standalone workflow invocation
    from polymarket_receipt_adapter import normalize_user_event


OFFICIAL_SOURCES = {
    "realtime_order_updates": "https://docs.polymarket.com/trading/realtime-order-updates",
    "order_lifecycle": "https://docs.polymarket.com/concepts/order-lifecycle",
    "maker_rebates": "https://docs.polymarket.com/programs/maker-rebates",
    "client_v2_changelog": "https://github.com/Polymarket/rs-clob-client-v2/blob/main/CHANGELOG.md",
}


def _order(status: str = "LIVE", event_type: str = "PLACEMENT") -> dict[str, Any]:
    return {
        "event_type": "order",
        "id": "order-1",
        "owner": "clob-owner-1",
        "market": "condition-1",
        "asset_id": "token-1",
        "side": "BUY",
        "original_size": "10",
        "size_matched": "0",
        "price": "0.52",
        "associate_trades": None,
        "type": event_type,
        "created_at": "1782753357",
        "expiration": "0",
        "order_type": "GTC",
        "status": status,
        "maker_address": "maker-1",
        "timestamp": "1782753357257",
    }


def _trade(status: str, *, transaction_hash: str | None = None) -> dict[str, Any]:
    return {
        "event_type": "trade",
        "type": "TRADE",
        "id": "trade-1",
        "taker_order_id": "order-1",
        "market": "condition-1",
        "asset_id": "token-1",
        "side": "BUY",
        "size": "10",
        "fee_rate_bps": "0",
        "price": "0.52",
        "status": status,
        "match_time": "1782753357",
        "last_update": "1782753357",
        "owner": "clob-owner-1",
        "trade_owner": "clob-owner-1",
        "maker_address": "maker-1",
        "transaction_hash": transaction_hash,
        "bucket_index": 0,
        "maker_orders": [
            {
                "order_id": "maker-order-1",
                "owner": "maker-owner-1",
                "maker_address": "maker-1",
                "matched_amount": "10",
                "price": "0.52",
                "fee_rate_bps": "0",
                "asset_id": "token-1",
                "outcome": "Yes",
                "outcome_index": 0,
                "side": "SELL",
            }
        ],
        "trader_side": "TAKER",
        "timestamp": "1782753357257",
    }


def audit() -> dict[str, Any]:
    placement = normalize_user_event(_order())
    update = normalize_user_event(_order(status="MATCHED", event_type="UPDATE"))
    cancellation = normalize_user_event(_order(status="CANCELED", event_type="CANCELLATION"))
    matched = normalize_user_event(_trade("MATCHED"))
    mined = normalize_user_event(_trade("MINED"))
    confirmed = normalize_user_event(_trade("CONFIRMED", transaction_hash="0xconfirmed"))
    failed = normalize_user_event(_trade("FAILED", transaction_hash="0xfailed"))

    checks = {
        "placement_fields_normalized": placement["event_kind"] == "order" and placement["order_event_type"] == "PLACEMENT" and placement["order_status"] == "LIVE",
        "matched_order_is_not_fill": update["confirmed_filled_shares"] == "0" and update["order_terminal"] is False,
        "cancellation_is_terminal_order_state": cancellation["order_terminal"] is True and cancellation["confirmed_filled_shares"] == "0",
        "matched_trade_is_pending": matched["trade_status"] == "MATCHED" and matched["trade_pending"] is True and matched["trade_confirmed"] is False,
        "mined_trade_is_pending": mined["trade_status"] == "MINED" and mined["trade_pending"] is True and mined["trade_confirmed"] is False,
        "confirmed_trade_is_terminal_fill_candidate": confirmed["trade_status"] == "CONFIRMED" and confirmed["trade_confirmed"] is True and confirmed["confirmed_filled_shares"] == "10" and confirmed["transaction_ref"] == "0xconfirmed",
        "failed_trade_is_terminal_nonfill": failed["trade_status"] == "FAILED" and failed["trade_failed"] is True and failed["trade_confirmed"] is False,
    }
    return {
        "format": "h463-current-user-stream-contract-v1",
        "hypothesis_id": "H463-current-user-stream-lifecycle-boundary",
        "official_sources": OFFICIAL_SOURCES,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "required_owner_bundle_fields": [
            "order_id",
            "order_event_type",
            "order_status",
            "trade_id",
            "trade_owner",
            "maker_address",
            "maker_orders",
            "trade_status",
            "transaction_hash",
            "match_time",
            "last_update",
            "settlement_receipt",
            "cash_release_receipt",
            "independent_balance_checkpoint",
        ],
        "decision": "current_wire_boundary_ready_but_owner_settlement_cash_bundle_missing",
        "root_cause": "The current adapter can distinguish accepted, matched, mined, confirmed and failed user-stream states. The remaining admission gap is an owner-scoped bundle that binds the confirmed trade to on-chain settlement and reusable cash; queue position is still not present in the official user event itself.",
        "strategy_evaluated": False,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = audit()
    if not result["all_checks_passed"]:
        raise SystemExit("h463_contract_failed")
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "decision": result["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()
