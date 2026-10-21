"""Audit H490's offline authenticated two-leg bundle contract.

The audit uses only synthetic rows to exercise the validator.  It does not
collect market data, fit a model, or create a live order.  A passing audit
means the contract rejects resets, partial legs and late cash release; it is
not a Canary result.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from validate_authenticated_maker_pair_bundle import validate_bundle_document


def _pair(index: int) -> dict[str, Any]:
    seq = index + 1
    before = Decimal("10") + Decimal("5") * index
    return {
        "owner": "synthetic-owner",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "sequence_no": seq,
        "previous_checkpoint_id": "opening" if index == 0 else f"balance-{index}",
        "status": "confirmed",
        "pair_id": f"pair-{seq}",
        "up_order_id": f"up-order-{seq}",
        "down_order_id": f"down-order-{seq}",
        "up_trade_id": f"up-trade-{seq}",
        "down_trade_id": f"down-trade-{seq}",
        "up_transaction_ref": f"up-tx-{seq}",
        "down_transaction_ref": f"down-tx-{seq}",
        "up_order_type": "GTC",
        "down_order_type": "GTC",
        "up_post_only": True,
        "down_post_only": True,
        "up_order_status": "MATCHED",
        "down_order_status": "MATCHED",
        "up_trade_status": "TRADE_STATUS_CONFIRMED",
        "down_trade_status": "TRADE_STATUS_CONFIRMED",
        "up_trader_side": "MAKER",
        "down_trader_side": "MAKER",
        "up_order_acceptance_ts": seq * 100 - 20,
        "down_order_acceptance_ts": seq * 100 - 20,
        "up_trade_matched_ts": seq * 100,
        "down_trade_matched_ts": seq * 100,
        "up_fee_rate_bps": "0",
        "down_fee_rate_bps": "0",
        "up_shares": "10",
        "down_shares": "10",
        "up_price": "0.25",
        "down_price": "0.25",
        "merge_receipt_id": f"merge-{seq}",
        "settlement_receipt_id": f"settlement-{seq}",
        "cash_release_receipt_id": f"cash-release-{seq}",
        "balance_checkpoint_id": f"balance-{seq}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": seq * 100,
        "cash_available_ts": seq * 100 + 10,
        "released_cash_since_previous_entry": "0" if index == 0 else "10",
        "balance_before": str(before),
        "balance_after": str(before - Decimal("5")),
        "entry_cost": "5",
        "fee": "0",
        "payout": "10",
    }


def _document() -> dict[str, Any]:
    return {
        "schema": "authenticated-maker-pair-bundle-v1",
        "owner": "synthetic-owner",
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "starting_checkpoint_id": "opening",
        "closing_balance_checkpoint_id": "closing",
        "initial_cash": "10",
        "closing_balance": "60",
        "events": [_pair(i) for i in range(10)],
    }


def audit() -> dict[str, Any]:
    valid = validate_bundle_document(_document())

    missing_leg_doc = _document()
    missing_leg_doc["events"][0].pop("down_trade_id")
    missing_leg = validate_bundle_document(missing_leg_doc)
    post_only_doc = _document()
    post_only_doc["events"][0]["down_post_only"] = False
    post_only_failure = validate_bundle_document(post_only_doc)
    late_release_doc = _document()
    late_release_doc["events"][0]["cash_available_ts"] = late_release_doc["events"][1]["entry_ts"]
    late_release = validate_bundle_document(late_release_doc)
    reset_doc = _document()
    reset_doc["events"][5]["ledger_id"] = "ledger-2"
    reset = validate_bundle_document(reset_doc)

    checks = {
        "ten_pair_chain_is_offline_only": valid.get("offline_consistency_passed") is True and valid.get("gate_passed") is False,
        "synthetic_rows_never_count_as_canary": valid.get("actual_fills") == 0 and valid.get("verified_canary_fills") == 0,
        "both_legs_required": missing_leg.get("failure_reason") == "down_trade_id_required",
        "both_legs_post_only_required": post_only_failure.get("failure_reason") == "down_post_only_required",
        "cash_release_precedes_next_entry": late_release.get("failure_reason") == "prior_cash_release_must_precede_next_entry",
        "ledger_reset_rejected": reset.get("failure_reason") == "ledger_id_mismatch",
    }
    return {
        "format": "h490-authenticated-maker-pair-bundle-v1",
        "hypothesis_id": "H490_AUTHENTICATED_TWO_LEG_MAKER_BUNDLE_CONTRACT",
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "offline_consistency_passed": bool(valid.get("offline_consistency_passed")),
        "paper_pairs": int(valid.get("pairs", 0)),
        "paper_ending_cash": valid.get("ending_cash"),
        "gate_passed": bool(valid.get("gate_passed")),
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "root_cause": "The paper pair replay has no owner-scoped order, confirmed maker trade, settlement, released-cash and independent balance bundle. H490 makes that missing evidence an explicit admission boundary while preserving the old cleaned-data replay as offline-only.",
        "decision": "keep_live_buying_paused_and_wait_for_owner_scoped_two_leg_bundle",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    if not result["all_checks_passed"]:
        raise SystemExit("h490_contract_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()

