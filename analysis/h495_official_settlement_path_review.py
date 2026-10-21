"""Record the official Polymarket V2 settlement-path evidence boundary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


FORMAT = "h495-official-settlement-path-review-v1"
SOURCES = {
    "exchange_v2": "https://github.com/Polymarket/ctf-exchange-v2",
    "trading_sol": "https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Trading.sol",
    "lifecycle": "https://github.com/Polymarket/polymarket-v2-external/blob/main/src/exchange/MATCH_ORDERS_LIFECYCLE.md",
    "order_lifecycle_docs": "https://docs.polymarket.com/concepts/order-lifecycle",
    "positions_docs": "https://docs.polymarket.com/trading/positions/manage",
}


def review() -> dict[str, object]:
    return {
        "format": FORMAT,
        "hypothesis_id": "H495_OFFICIAL_SETTLEMENT_PATH_BINDING",
        "source_urls": SOURCES,
        "source_version": "Polymarket CTF Exchange V2",
        "polygon_exchange_contracts": {
            "ctf_exchange_v2": "0xE111180000d2663C0091e4f400237545B87B996B",
            "neg_risk_ctf_exchange_v2": "0xe2222d279d744050d28e00520010520000310F59",
        },
        "match_paths": ["COMPLEMENTARY", "MINT", "MERGE"],
        "maker_order_filled_emitted_before_batch_ctf_operation": True,
        "batch_ctf_operations": ["SPLIT", "MERGE"],
        "taker_settlement_event_follows_settlement": True,
        "future_owner_bundle_required_fields": [
            "exchange_version_and_contract",
            "match_path",
            "owner_scoped_maker_order_filled_event",
            "owner_scoped_asset_transfer_events",
            "ctf_split_merge_or_complementary_path_evidence",
            "owner_scoped_redeem_or_payout_receipt",
            "cash_token_balance_before_after_and_next_entry",
        ],
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "canary_gate_passed": False,
        "decision": "bind_future_settlement_parser_to_exchange_version_and_match_path",
        "root_cause": "The existing 47 trade receipts establish payment transfers but do not identify a complete CTF settlement path and cash-token release chain for ten chronological fills.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = review()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": FORMAT, "canary_gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
