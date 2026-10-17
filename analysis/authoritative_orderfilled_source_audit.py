"""Audit whether existing archives can support an authoritative execution join.

This is deliberately a schema and artifact audit.  It never downloads data,
contacts a wallet, submits an order, or treats a paper outcome as a fill.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "order_id",
    "trade_id",
    "transaction_hash",
    "owner",
    "maker",
    "taker",
    "queue_position",
    "fill",
    "settlement",
    "cash_release",
    "trade_count_1s",
    "trade_volume_1s",
}


def csv_schema(root: Path) -> dict[str, Any]:
    files = sorted(root.rglob("*.csv"))
    union: set[str] = set()
    shapes: dict[str, int] = {}
    readable = 0
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                fields = tuple(next(csv.reader(handle)))
        except (OSError, StopIteration, csv.Error):
            continue
        readable += 1
        union.update(fields)
        shapes[",".join(fields)] = shapes.get(",".join(fields), 0) + 1
    lower = {field.lower() for field in union}
    return {
        "csv_files": len(files),
        "readable_csv_files": readable,
        "distinct_header_shapes": len(shapes),
        "fields": sorted(union),
        "required_field_presence": {field: field.lower() in lower for field in sorted(REQUIRED_FIELDS)},
        "authoritative_execution_fields_present": any(
            field.lower() in lower
            for field in ("order_id", "trade_id", "transaction_hash", "queue_position", "settlement", "cash_release")
        ),
    }


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, child
            yield from _walk(child, child_path)


def current_artifact_audit(root: Path) -> dict[str, Any]:
    json_files = sorted(root.rglob("*.json"))
    key_paths: set[str] = set()
    receipt_flags: list[dict[str, Any]] = []
    for path in json_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for key_path, value in _walk(payload):
            key_paths.add(key_path.split(".")[-1].split("[")[0])
            if key_path.endswith("execution_receipts_present"):
                receipt_flags.append({"file": str(path), "value": value})
            if key_path.endswith("execution_receipt_snapshot_rows"):
                receipt_flags.append({"file": str(path), "value": value})
    lower_keys = {key.lower() for key in key_paths}
    return {
        "json_files": len(json_files),
        "key_names": sorted(key_paths),
        "required_field_name_presence": {field: field.lower() in lower_keys for field in sorted(REQUIRED_FIELDS)},
        "execution_receipt_flags": receipt_flags,
        "authoritative_execution_fields_present": any(
            field.lower() in lower_keys
            for field in ("order_id", "trade_id", "transaction_hash", "queue_position", "settlement", "cash_release")
        ),
    }


def audit(old_root: Path, current_root: Path) -> dict[str, Any]:
    old = csv_schema(old_root)
    current = current_artifact_audit(current_root)
    return {
        "schema_version": 1,
        "old_clean_source": str(old_root),
        "current_capture_artifacts": str(current_root),
        "old_clean": old,
        "current_capture": current,
        "orderfilled_join_ready": bool(
            old["authoritative_execution_fields_present"] or current["authoritative_execution_fields_present"]
        ),
        "execution_receipts_verified": any(item.get("value") is True for item in current["execution_receipt_flags"]),
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "decision": "source_insufficient_for_authoritative_orderfilled_replay",
        "root_cause": "Existing clean CSVs and current capture summaries expose quote/label fields but no authoritative order, trade, queue, transaction, settlement, or cash-release records.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-root", required=True, type=Path)
    parser.add_argument("--current-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.old_root, args.current_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"orderfilled_join_ready": result["orderfilled_join_ready"], "execution_receipts_verified": result["execution_receipts_verified"], "old_clean_csv_files": result["old_clean"]["csv_files"]}, sort_keys=True))


if __name__ == "__main__":
    main()
