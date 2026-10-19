"""Bounded provenance audit for raw BTC 5m OrderFilled windows.

This experiment does not infer a trading edge.  It verifies whether a fixed,
public BTC5m source preserves transaction/order identity, asset amounts, fee,
and causal timestamps well enough to register a later execution-aware test.
The source is a third-party release of raw v1 OrderFilled events; it is not an
owner-scoped wallet ledger and cannot create a fill, settlement, or cash receipt.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from pathlib import Path
from typing import Any


DATASET_REVISION = "f3bfb79f19611ea3115ad29c18720166ee6dd0b5"
DATASET_URL = "https://huggingface.co/datasets/krish301/polymarket-crypto-trades-v1"
SOURCE_README = DATASET_URL + "/blob/" + DATASET_REVISION + "/README.md"
WINDOW_START = 1772668800  # 2026-03-05 00:00 UTC
WINDOW_SECONDS = 300
WINDOW_COUNT = 12
V1_EXCHANGE_TAKERS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
}
HEX64 = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
REQUIRED = {
    "id", "timestamp", "transactionHash", "orderHash", "maker", "taker",
    "makerAssetId", "takerAssetId", "makerAmountFilled",
    "takerAmountFilled", "fee",
}


def expected_window_starts() -> list[int]:
    return [WINDOW_START + WINDOW_SECONDS * i for i in range(WINDOW_COUNT)]


def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean_integer")
    result = int(str(value))
    if result < 0:
        raise ValueError("negative_integer")
    return result


def _window_rows(path: Path, window_start: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    txs: set[str] = set()
    orders: set[str] = set()
    tx_exchange: set[str] = set()
    invalid: list[str] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not REQUIRED.issubset(row):
                    raise ValueError("missing_fields")
                if not HEX64.fullmatch(str(row["transactionHash"])):
                    raise ValueError("transaction_hash")
                if not HEX64.fullmatch(str(row["orderHash"])):
                    raise ValueError("order_hash")
                if not ADDRESS.fullmatch(str(row["maker"])) or not ADDRESS.fullmatch(str(row["taker"])):
                    raise ValueError("address")
                row_id = str(row["id"])
                if not row_id or row_id in ids:
                    raise ValueError("duplicate_id")
                timestamp = _int(row["timestamp"])
                if not window_start <= timestamp < window_start + WINDOW_SECONDS:
                    raise ValueError("timestamp_out_of_window")
                maker_asset = str(row["makerAssetId"])
                taker_asset = str(row["takerAssetId"])
                if (maker_asset == "0") == (taker_asset == "0"):
                    raise ValueError("asset_pair_not_binary")
                if maker_asset != "0" and taker_asset != "0":
                    raise ValueError("missing_usdc_leg")
                if maker_asset == "0" and not taker_asset.isdigit():
                    raise ValueError("bad_token_id")
                if taker_asset == "0" and not maker_asset.isdigit():
                    raise ValueError("bad_token_id")
                _int(row["makerAmountFilled"])
                _int(row["takerAmountFilled"])
                _int(row["fee"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                invalid.append(f"{line_no}:{exc}")
                continue
            ids.add(row_id)
            tx = str(row["transactionHash"]).lower()
            order = str(row["orderHash"]).lower()
            txs.add(tx)
            orders.add(order)
            if str(row["taker"]).lower() in V1_EXCHANGE_TAKERS:
                tx_exchange.add(tx)
            rows.append({
                "timestamp": timestamp,
                "transaction_hash": tx,
                "order_hash": order,
                "maker": str(row["maker"]).lower(),
                "taker": str(row["taker"]).lower(),
                "maker_asset_id": str(row["makerAssetId"]),
                "taker_asset_id": str(row["takerAssetId"]),
                "maker_amount_filled": str(row["makerAmountFilled"]),
                "taker_amount_filled": str(row["takerAmountFilled"]),
                "fee": str(row["fee"]),
                "exchange_taker_leg": str(row["taker"]).lower() in V1_EXCHANGE_TAKERS,
            })
    return rows, {
        "window_start": window_start,
        "path": path.name,
        "raw_rows": len(rows) + len(invalid),
        "valid_rows": len(rows),
        "invalid_rows": len(invalid),
        "invalid_examples": invalid[:3],
        "unique_transaction_hashes": len(txs),
        "unique_order_hashes": len(orders),
        "transactions_with_exchange_taker_leg": len(tx_exchange),
        "all_valid": not invalid,
    }


def audit(input_dir: Path, output_dir: Path) -> dict[str, Any]:
    starts = expected_window_starts()
    window_metrics: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    missing: list[int] = []
    for start in starts:
        path = input_dir / f"{start}.fills.jsonl.gz"
        if not path.is_file():
            missing.append(start)
            continue
        rows, metrics = _window_rows(path, start)
        window_metrics.append(metrics)
        all_rows.extend(rows)

    rows_total = len(all_rows)
    tx_total = len({r["transaction_hash"] for r in all_rows})
    tx_with_exchange = len({r["transaction_hash"] for r in all_rows if r["exchange_taker_leg"]})
    total_invalid = sum(int(x["invalid_rows"]) for x in window_metrics)
    report = {
        "format": "h325-orderfilled-btc5m-source-audit-v1",
        "hypothesis_ids": [
            "H325A-raw-orderfilled-identity-coverage",
            "H325B-early-flow-fields-admit-causal-execution-test",
        ],
        "dataset_url": DATASET_URL,
        "dataset_revision": DATASET_REVISION,
        "source_readme": SOURCE_README,
        "source_scope": "fixed public raw v1 OrderFilled BTC5m windows; no wallet or balance scope",
        "window_start": WINDOW_START,
        "window_count_requested": len(starts),
        "window_count_observed": len(window_metrics),
        "missing_window_count": len(missing),
        "missing_window_starts": missing,
        "rows_total": rows_total,
        "unique_transaction_hashes": tx_total,
        "unique_order_hashes": len({r["order_hash"] for r in all_rows}),
        "transactions_with_exchange_taker_leg": tx_with_exchange,
        "exchange_taker_transaction_coverage": (tx_with_exchange / tx_total) if tx_total else 0.0,
        "invalid_rows": total_invalid,
        "all_observed_windows_valid": bool(window_metrics) and not missing and total_invalid == 0,
        "fees_present": all(bool(str(r["fee"])) for r in all_rows),
        "causal_timestamps_present": bool(all_rows),
        "official_outcome_or_settlement_fields_in_fill_rows": False,
        "owner_scoped_cash_fields": False,
        "h325a_identity_coverage_passed": bool(window_metrics) and not missing and total_invalid == 0 and tx_with_exchange == tx_total,
        "h325b_execution_fields_available_for_registration": bool(all_rows) and total_invalid == 0,
        "strategy_evaluated": False,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": True,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "source_contract_audited_no_strategy_promotion",
        "root_cause_or_limit": "Raw OrderFilled identity improves causal execution provenance, but this source alone lacks owner-scoped settlement, cash release and reusable balance continuity.",
        "next_action": "Use only the verified event fields to register one predeclared signed-flow paper test; require independent settlement and cash checkpoints before any Canary admission.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with (output_dir / "window_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["window_start", "path", "raw_rows", "valid_rows", "invalid_rows", "unique_transaction_hashes", "unique_order_hashes", "transactions_with_exchange_taker_leg", "all_valid"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in window_metrics)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.input_dir, args.output_dir)
    print(json.dumps({
        "format": report["format"],
        "decision": report["decision"],
        "strategy_evaluated": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
