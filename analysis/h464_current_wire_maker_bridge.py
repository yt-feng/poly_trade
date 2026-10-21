"""Bridge the current user-stream wire shape into the maker ledger adapter.

This is a synthetic integration contract.  It uses the documented order/trade
shape only to verify that a future owner-scoped bundle can flow through the
current wire normalizer, maker receipt adapter and one-ledger row assembler.
It never authenticates the synthetic identities and never counts a fill.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

try:
    from analysis.h463_current_user_stream_contract import _order, _trade
    from analysis.maker_compound_bundle_adapter import assemble_maker_compound_row
    from analysis.polymarket_receipt_adapter import adapt_order, adapt_trade
except ModuleNotFoundError:  # pragma: no cover - standalone workflow invocation
    from h463_current_user_stream_contract import _order, _trade
    from maker_compound_bundle_adapter import assemble_maker_compound_row
    from polymarket_receipt_adapter import adapt_order, adapt_trade


EXPECTED = {
    "market_id": "condition-1",
    "token_id": "token-1",
    "owner": "clob-owner-1",
    "order_id": "maker-order-1",
}


def audit() -> dict[str, Any]:
    order_payload = _order(status="MATCHED", event_type="UPDATE")
    order_payload["id"] = "maker-order-1"
    order_payload["tradeIDs"] = ["trade-1"]
    order_payload["size_matched"] = "10"
    trade_payload = _trade("CONFIRMED", transaction_hash="0xconfirmed")
    trade_payload["trader_side"] = "MAKER"
    trade_payload["maker_orders"][0]["owner"] = "clob-owner-1"

    order_result = adapt_order(order_payload, expected=EXPECTED)
    trade_result = adapt_trade(
        trade_payload,
        expected=EXPECTED,
        order_id="maker-order-1",
        required_trader_side="MAKER",
    )
    order_event = order_result["events"][0] if order_result["events"] else {}
    trade_event = trade_result["events"][0] if trade_result["events"] else {}
    ledger_fields = {
        "sequence_no": 1,
        "ledger_id": "synthetic-ledger",
        "chain_id": "synthetic-chain",
        "orderfilled_event_id": "0xconfirmed:1",
        "previous_checkpoint_id": "opening",
        "balance_checkpoint_id": "balance-1",
        "independent_checkpoint": True,
        "cash_reconciled": True,
        "entry_ts": "100",
        "cash_available_ts": "110",
        "entry_cost": "5.20",
        "cash_release_receipt_id": "cash-1",
        "settlement_receipt_id": "settlement-1",
        "balance_before": "10",
        "balance_after": "10",
        "released_cash_since_previous_entry": "0",
    }
    settlement_fields = {
        "settlement_status": "CONFIRMED",
        "settlement_payout": "5.20",
        "payout": "5.20",
    }
    rebate_fields = {
        "rebate_status": "NOT_ELIGIBLE",
        "rebate_amount": "0",
        "fee": "0",
    }
    assembled: dict[str, Any] = {}
    assemble_error = None
    if not order_result["issues"] and not trade_result["issues"]:
        try:
            assembled = assemble_maker_compound_row(
                order_event=order_event,
                trade_event=trade_event,
                ledger_fields=ledger_fields,
                settlement_fields=settlement_fields,
                rebate_fields=rebate_fields,
                post_only_accepted=True,
                order_updates=[
                    {"kind": "entry_order_accepted", "order_id": "maker-order-1", "trade_ids": []},
                    {"kind": "entry_order_accepted", "order_id": "maker-order-1", "trade_ids": ["trade-1"]},
                ],
            )
        except (TypeError, ValueError) as exc:
            assemble_error = str(exc)

    checks = {
        "order_adapter_accepts_current_wire": not order_result["issues"] and bool(order_event),
        "maker_trade_adapter_accepts_current_wire": not trade_result["issues"] and bool(trade_event),
        "async_trade_id_binding_ready": assembled.get("async_trade_id_binding_ready") is True,
        "maker_row_has_owner_order_trade_links": all(
            assembled.get(field) for field in ("owner", "order_id", "trade_id", "transaction_ref", "orderfilled_event_id")
        ),
        "maker_row_is_not_authenticated": True,
    }
    return {
        "format": "h464-current-wire-maker-bridge-v1",
        "hypothesis_id": "H464-current-wire-to-maker-ledger-bridge",
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "assemble_error": assemble_error,
        "row_field_count": len(assembled),
        "required_future_bundle_fields": [
            "order_acceptance",
            "trade_id",
            "transaction_hash",
            "maker_orderfilled_binding",
            "queue_or_fill_lifecycle_evidence",
            "settlement_receipt",
            "cash_release_receipt",
            "independent_balance_checkpoint",
        ],
        "decision": "wire_to_maker_row_bridge_ready_but_external_receipts_missing",
        "root_cause": "The local integration path is ready for current user-stream shapes; admission still depends on externally authenticated settlement, released cash and independent balance continuity.",
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
        raise SystemExit("h464_bridge_failed")
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "decision": result["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()
