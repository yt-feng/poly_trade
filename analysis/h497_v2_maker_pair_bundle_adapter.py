"""Synthetic H497 audit for the integrated owner-pair/V2 path adapter."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    from .authenticated_v2_maker_pair_bundle_adapter import assemble_authenticated_v2_maker_pair_event
    from .validate_authenticated_maker_pair_bundle import validate_bundle_document
    from .validate_v2_settlement_path import validate_v2_path_bundle
except ImportError:  # pragma: no cover - direct script execution
    from authenticated_v2_maker_pair_bundle_adapter import assemble_authenticated_v2_maker_pair_event
    from validate_authenticated_maker_pair_bundle import validate_bundle_document
    from validate_v2_settlement_path import validate_v2_path_bundle


CONTRACT = "0xE111180000d2663C0091e4f400237545B87B996B"


def _leg(prefix: str, sequence: int) -> tuple[dict[str, Any], dict[str, Any]]:
    order_id = f"{prefix}-order-{sequence}"
    return (
        {"order_id": order_id, "owner": "synthetic-owner", "order_type": "GTC", "post_only": True, "order_status": "MATCHED", "order_acceptance_ts": sequence * 100 - 20},
        {"owner": "synthetic-owner", "trade_owner": "synthetic-owner", "trade_id": f"{prefix}-trade-{sequence}", "transaction_ref": f"{prefix}-tx-{sequence}", "market_id": "market-1", "asset_id": f"{prefix}-asset-{sequence}", "shares": "10", "price": "0.25", "fee_rate_bps": "0", "side": "BUY", "execution_role": "MAKER", "trader_side": "MAKER", "trade_status": "TRADE_STATUS_CONFIRMED", "trade_matched_ts": sequence * 100, "maker_order_ids": [order_id]},
    )


def _event(sequence: int) -> dict[str, Any]:
    up_order, up_trade = _leg("up", sequence)
    down_order, down_trade = _leg("down", sequence)
    before = Decimal("10") + Decimal("5") * (sequence - 1)
    pair_fields = {
        "owner": "synthetic-owner", "ledger_id": "ledger-1", "chain_id": "polygon", "market_id": "market-1", "pair_id": f"pair-{sequence}", "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}", "entry_ts": sequence * 100, "cash_available_ts": sequence * 100 + 30,
        "released_cash_since_previous_entry": "0" if sequence == 1 else "10", "balance_before": str(before), "balance_after": str(before - Decimal("5")), "entry_cost": "5", "fee": "0", "payout": "10",
        "balance_checkpoint_id": f"balance-{sequence}", "cash_reconciled": True, "independent_checkpoint": True,
    }
    return assemble_authenticated_v2_maker_pair_event(
        up_order_event=up_order, up_trade_event=up_trade, down_order_event=down_order, down_trade_event=down_trade,
        pair_fields=pair_fields,
        settlement_fields={"merge_receipt_id": f"merge-{sequence}", "settlement_receipt_id": f"settlement-{sequence}", "cash_release_receipt_id": f"cash-{sequence}"},
        path_fields={
            "exchange_version": "ctf-exchange-v2", "exchange_contract": CONTRACT, "match_path": "COMPLEMENTARY",
            "maker_order_filled_refs": [f"filled-up-{sequence}", f"filled-down-{sequence}"], "asset_transfer_refs": [f"transfer-up-{sequence}", f"transfer-down-{sequence}"],
            "payout_receipt_id": f"payout-{sequence}", "cash_release_receipt_id": f"cash-{sequence}", "cash_token_balance_checkpoint_id": f"cash-balance-{sequence}",
            "maker_order_filled_ts": sequence * 100 - 10, "payout_ts": sequence * 100 + 20, "cash_available_ts": sequence * 100 + 30,
            "cash_token_balance_before": str(10 + sequence - 1), "cash_token_balance_after": str(10 + sequence),
        },
    )


def audit() -> dict[str, Any]:
    events = [_event(i) for i in range(1, 11)]
    h490 = validate_bundle_document({"schema": "authenticated-maker-pair-bundle-v1", "owner": "synthetic-owner", "ledger_id": "ledger-1", "chain_id": "polygon", "starting_checkpoint_id": "opening", "closing_balance_checkpoint_id": "closing", "initial_cash": "10", "closing_balance": "60", "events": events})
    h496 = validate_v2_path_bundle({"schema": "authenticated-maker-pair-v2-path-bundle-v1", "owner": "synthetic-owner", "ledger_id": "ledger-1", "chain_id": "polygon", "events": events})
    bad = dict(events[0]); bad["cash_available_ts"] = bad["payout_ts"] - 1
    checks = {
        "h490_pair_ledger_validates_offline": h490.get("offline_consistency_passed") is True,
        "h496_v2_path_validates_offline": h496.get("offline_consistency_passed") is True,
        "integrated_rows_never_count_as_canary": h490.get("gate_passed") is False and h496.get("verified_canary_fills") == 0,
        "path_clock_rejects_bad_row": validate_v2_path_bundle({"schema": "authenticated-maker-pair-v2-path-bundle-v1", "owner": "synthetic-owner", "ledger_id": "ledger-1", "chain_id": "polygon", "events": [bad, *events[1:]]}).get("failure_reason") == "cash_available_before_payout",
    }
    return {"format": "h497-v2-maker-pair-bundle-adapter-v1", "hypothesis_id": "H497_INTEGRATED_V2_MAKER_PAIR_BUNDLE_ADAPTER", "checks": checks, "all_checks_passed": all(checks.values()), "h490_offline_consistency_passed": bool(h490.get("offline_consistency_passed")), "h496_offline_consistency_passed": bool(h496.get("offline_consistency_passed")), "paper_pairs": 10, "paper_ending_cash": "60", "gate_passed": False, "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0, "new_market_data_collected": False, "model_refit": False, "live_orders_enabled": False, "decision": "integrated_adapter_ready_but_owner_receipt_authentication_pending", "root_cause": "The pair adapter and V2 path contract were previously separate; H497 joins them while retaining the offline-only evidence boundary."}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args(); result = audit()
    if not result["all_checks_passed"]: raise SystemExit("h497_integrated_adapter_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
