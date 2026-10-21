"""Audit the strict two-leg maker receipt contract over synthetic evidence.

H488 is a contract repair, not a market experiment.  It proves that the
future owner-scoped pair adapter requires the full order/trade lifecycle for
both legs, while a synthetically consistent ten-pair chain remains offline
only and cannot set the authenticated Canary gate.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    from .maker_pair_receipt_contract import validate_maker_pair_chain
except ImportError:  # pragma: no cover
    from maker_pair_receipt_contract import validate_maker_pair_chain


def _pair(index: int) -> dict[str, Any]:
    sequence = index + 1
    before = Decimal("10") + Decimal("0.50") * index
    return {
        "owner": "synthetic-owner",
        "status": "confirmed",
        "sequence_no": sequence,
        "pair_id": f"pair-{sequence}",
        "up_order_id": f"up-order-{sequence}",
        "down_order_id": f"down-order-{sequence}",
        "up_trade_id": f"up-trade-{sequence}",
        "down_trade_id": f"down-trade-{sequence}",
        "up_transaction_ref": f"up-tx-{sequence}",
        "down_transaction_ref": f"down-tx-{sequence}",
        "up_trader_side": "MAKER",
        "down_trader_side": "MAKER",
        "up_order_type": "GTC",
        "down_order_type": "GTC",
        "up_post_only": True,
        "down_post_only": True,
        "up_order_status": "MATCHED",
        "down_order_status": "MATCHED",
        "up_trade_status": "TRADE_STATUS_CONFIRMED",
        "down_trade_status": "TRADE_STATUS_CONFIRMED",
        "up_order_acceptance_ts": str(sequence * 100 - 20),
        "down_order_acceptance_ts": str(sequence * 100 - 20),
        "up_trade_matched_ts": str(sequence * 100),
        "down_trade_matched_ts": str(sequence * 100),
        "up_fee_rate_bps": "0",
        "down_fee_rate_bps": "0",
        "merge_receipt_id": f"merge-{sequence}",
        "settlement_receipt_id": f"settlement-{sequence}",
        "cash_release_receipt_id": f"cash-{sequence}",
        "balance_checkpoint_id": f"balance-{sequence}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(sequence * 100),
        "cash_available_ts": str(sequence * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
    }


def audit() -> dict[str, Any]:
    valid = validate_maker_pair_chain([_pair(i) for i in range(10)], owner="synthetic-owner")
    missing_post_only = _pair(0)
    missing_post_only["down_post_only"] = False
    lifecycle_failure = validate_maker_pair_chain([missing_post_only], owner="synthetic-owner")
    late_match = _pair(0)
    late_match["up_trade_matched_ts"] = "101"
    timestamp_failure = validate_maker_pair_chain([late_match], owner="synthetic-owner")
    checks = {
        "complete_chain_is_offline_only": valid.get("offline_consistency_passed") is True and valid.get("gate_passed") is False,
        "synthetic_chain_never_counts_canary": valid.get("actual_fills") == 0 and valid.get("verified_canary_fills") == 0,
        "both_leg_post_only_required": lifecycle_failure.get("failure_reason") == "down_post_only_required",
        "both_leg_match_time_precedes_entry": timestamp_failure.get("failure_reason") == "up_trade_match_after_entry",
    }
    return {
        "format": "h488-strict-paired-maker-contract-v1",
        "hypothesis_id": "H488_STRICT_PAIRED_MAKER_RECEIPT_GATE",
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "offline_consistency_passed": bool(valid.get("offline_consistency_passed")),
        "paper_pairs": int(valid.get("fills", 0)),
        "gate_passed": bool(valid.get("gate_passed")),
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "root_cause": "The legacy pair contract could return gate_passed for synthetic rows without proving both legs' post-only confirmed lifecycle. H488 requires that lifecycle and separates offline consistency from authenticated Canary admission.",
        "decision": "contract_repaired_keep_live_buying_paused_until_owner_scoped_two_leg_settlement_cash_bundle",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    if not result["all_checks_passed"]:
        raise SystemExit("h488_contract_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
