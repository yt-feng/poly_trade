"""Synthetic H496 audit for the official V2 settlement-path contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .validate_v2_settlement_path import validate_v2_path_bundle
except ImportError:  # pragma: no cover - direct script execution
    from validate_v2_settlement_path import validate_v2_path_bundle


CONTRACT = "0xE111180000d2663C0091e4f400237545B87B996B"


def _event(sequence: int, *, path: str = "COMPLEMENTARY") -> dict[str, Any]:
    row = {
        "owner": "synthetic-owner",
        "ledger_id": "ledger-1",
        "chain_id": "polygon",
        "sequence_no": sequence,
        "exchange_version": "ctf-exchange-v2",
        "exchange_contract": CONTRACT,
        "match_path": path,
        "maker_order_filled_refs": [f"filled-up-{sequence}", f"filled-down-{sequence}"],
        "asset_transfer_refs": [f"transfer-up-{sequence}", f"transfer-down-{sequence}"],
        "payout_receipt_id": f"payout-{sequence}",
        "cash_release_receipt_id": f"cash-{sequence}",
        "cash_token_balance_checkpoint_id": f"balance-{sequence}",
        "entry_ts": sequence * 100,
        "maker_order_filled_ts": sequence * 100 - 10,
        "payout_ts": sequence * 100 + 20,
        "cash_available_ts": sequence * 100 + 30,
        "cash_token_balance_before": str(10 + sequence - 1),
        "cash_token_balance_after": str(10 + sequence),
    }
    if path in {"MINT", "MERGE"}:
        row["ctf_operation_ref"] = f"ctf-{path.lower()}-{sequence}"
    return row


def audit() -> dict[str, Any]:
    document = {
        "schema": "authenticated-maker-pair-v2-path-bundle-v1",
        "owner": "synthetic-owner",
        "ledger_id": "ledger-1",
        "chain_id": "polygon",
        "events": [_event(sequence) for sequence in range(1, 11)],
    }
    valid = validate_v2_path_bundle(document)
    mint = dict(document)
    mint["events"] = [_event(sequence, path="MINT") for sequence in range(1, 11)]
    mint["events"][0]["ctf_operation_ref"] = ""
    mint_failure = validate_v2_path_bundle(mint)
    late = dict(document)
    late["events"] = [dict(row) for row in document["events"]]
    late["events"][0]["cash_available_ts"] = 105
    late_failure = validate_v2_path_bundle(late)
    checks = {
        "ten_path_rows_validate_offline": valid.get("offline_consistency_passed") is True,
        "synthetic_rows_never_count_as_canary": valid.get("gate_passed") is False and valid.get("verified_canary_fills") == 0,
        "mint_requires_ctf_operation": mint_failure.get("failure_reason") == "ctf_operation_ref_required",
        "cash_release_must_follow_payout": late_failure.get("failure_reason") == "cash_available_before_payout",
    }
    return {
        "format": "h496-v2-settlement-path-contract-v1",
        "hypothesis_id": "H496_OFFICIAL_V2_SETTLEMENT_PATH_CONTRACT",
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "offline_consistency_passed": bool(valid.get("offline_consistency_passed")),
        "match_paths": valid.get("match_paths"),
        "events": int(valid.get("events", 0)),
        "gate_passed": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "v2_path_contract_ready_but_keep_canary_gate_closed",
        "root_cause": "The existing owner evidence lacks a ten-row exchange/path-bound settlement and cash-release bundle; synthetic path consistency cannot authenticate receipts.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    if not result["all_checks_passed"]:
        raise SystemExit("h496_v2_path_contract_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
