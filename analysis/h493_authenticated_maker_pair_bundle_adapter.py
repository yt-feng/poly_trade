"""Audit H493's two-leg authenticated maker bundle adapter."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from authenticated_maker_pair_bundle_adapter import assemble_authenticated_maker_pair_event
from validate_authenticated_maker_pair_bundle import validate_bundle_document


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
    return assemble_authenticated_maker_pair_event(
        up_order_event=up_order,
        up_trade_event=up_trade,
        down_order_event=down_order,
        down_trade_event=down_trade,
        pair_fields={
            "owner": "synthetic-owner", "ledger_id": "ledger-1", "chain_id": "chain-1", "market_id": "market-1", "pair_id": f"pair-{sequence}", "sequence_no": sequence,
            "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}", "entry_ts": sequence * 100, "cash_available_ts": sequence * 100 + 10,
            "released_cash_since_previous_entry": "0" if sequence == 1 else "10", "balance_before": str(before), "balance_after": str(before - Decimal("5")), "entry_cost": "5", "fee": "0", "payout": "10",
            "balance_checkpoint_id": f"balance-{sequence}", "cash_reconciled": True, "independent_checkpoint": True,
        },
        settlement_fields={"merge_receipt_id": f"merge-{sequence}", "settlement_receipt_id": f"settlement-{sequence}", "cash_release_receipt_id": f"cash-{sequence}"},
    )


def audit() -> dict[str, Any]:
    document = {"schema": "authenticated-maker-pair-bundle-v1", "owner": "synthetic-owner", "ledger_id": "ledger-1", "chain_id": "chain-1", "starting_checkpoint_id": "opening", "closing_balance_checkpoint_id": "closing", "initial_cash": "10", "closing_balance": "60", "events": [_event(i) for i in range(1, 11)]}
    valid = validate_bundle_document(document)
    late = dict(document)
    late["events"] = [dict(row) for row in document["events"]]
    late["events"][0]["up_trade_matched_ts"] = 101
    late_result = validate_bundle_document(late)
    checks = {
        "adapter_builds_both_maker_legs": valid.get("offline_consistency_passed") is True,
        "adapter_output_never_counts_canary": valid.get("gate_passed") is False and valid.get("actual_fills") == 0 and valid.get("verified_canary_fills") == 0,
        "late_match_rejected": late_result.get("failure_reason") == "up_trade_match_after_entry",
    }
    return {
        "format": "h493-authenticated-maker-pair-bundle-adapter-v1",
        "hypothesis_id": "H493_AUTHENTICATED_MAKER_PAIR_BUNDLE_ADAPTER",
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "offline_consistency_passed": bool(valid.get("offline_consistency_passed")),
        "paper_pairs": int(valid.get("pairs", 0)),
        "paper_ending_cash": valid.get("ending_cash"),
        "gate_passed": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "adapter_ready_for_future_owner_bundle_but_keep_canary_gate_closed",
        "root_cause": "Before H493, two authenticated maker legs still required manual field joins before the strict pair validator could consume them; manual joins are a source of identity and timestamp ambiguity.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    if not result["all_checks_passed"]:
        raise SystemExit("h493_adapter_audit_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()

