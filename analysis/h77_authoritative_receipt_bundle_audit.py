"""Audit the old-clean CSV schema for authoritative execution evidence.

This is intentionally a header-only, read-only audit.  The old-clean source is
large and contains quote snapshots, so scanning every row would add heat without
answering whether the data can carry an owner-scoped fill/settlement/cash chain.
No quote row is promoted to a fill by this module.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
RECEIPT_FIELDS = {
    "owner",
    "account_id",
    "order_id",
    "trade_id",
    "transaction_hash",
    "transactionhash",
    "settlement_receipt_id",
    "settlement_receipt",
    "cash_release_ms",
    "cash_available_ms",
    "reusable_cash_ms",
}
QUOTE_LABEL_FIELDS = {"target_price", "final_price"}


def _header(path: Path) -> tuple[str, ...]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.reader([handle.readline()]), [])
    return tuple(str(value).strip().lower() for value in row if str(value).strip())


def audit(source_root: Path, *, source_commit: str = SOURCE_COMMIT) -> dict[str, Any]:
    files = sorted(path for path in source_root.rglob("*") if path.is_file() and path.suffix.lower() == ".csv")
    signatures: Counter[tuple[str, ...]] = Counter()
    receipt_presence: Counter[str] = Counter()
    quote_presence: Counter[str] = Counter()
    malformed_headers = 0
    header_digest = hashlib.sha256()
    for path in files:
        header = _header(path)
        if not header:
            malformed_headers += 1
        signatures[header] += 1
        header_digest.update(("\0".join(header) + "\n").encode("utf-8"))
        for field in RECEIPT_FIELDS & set(header):
            receipt_presence[field] += 1
        for field in QUOTE_LABEL_FIELDS & set(header):
            quote_presence[field] += 1
    header_sets = [list(signature) for signature in sorted(signatures)]
    receipt_files = sum(receipt_presence.values())
    result = {
        "format": "old-clean-h77-authoritative-receipt-bundle-audit-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "scope": "header-only schema audit; no row replay and no model change",
        "csv_files_scanned": len(files),
        "malformed_or_empty_headers": malformed_headers,
        "distinct_header_signatures": len(signatures),
        "header_signature_counts": [
            {"count": count, "fields": list(signature)}
            for signature, count in sorted(signatures.items(), key=lambda item: (item[0], item[1]))
        ],
        "header_multiset_sha256": header_digest.hexdigest(),
        "authoritative_receipt_field_file_counts": dict(sorted(receipt_presence.items())),
        "authoritative_receipt_files": receipt_files,
        "quote_label_field_file_counts": dict(sorted(quote_presence.items())),
        "authoritative_orderfilled_present": bool(receipt_presence),
        "settlement_receipt_present": any(
            field in receipt_presence for field in {"settlement_receipt_id", "settlement_receipt"}
        ),
        "cash_release_checkpoint_present": any(
            field in receipt_presence for field in {"cash_release_ms", "cash_available_ms", "reusable_cash_ms"}
        ),
        "decision": "source_insufficient_for_authoritative_receipt_chain",
        "root_cause": (
            "The old-clean CSV projection is a quote/depth snapshot schema. It exposes target_price/final_price "
            "labels in some files but has no owner, order/trade identity, transaction hash, settlement receipt "
            "or reusable-cash timestamp field, so it cannot prove a same-ledger authenticated fill."
        ),
        "evidence_boundary": "Schema evidence only; quote labels and markouts are not fills or settlement cash.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.source_root, source_commit=args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "csv_files_scanned": result["csv_files_scanned"],
        "distinct_header_signatures": result["distinct_header_signatures"],
        "authoritative_receipt_files": result["authoritative_receipt_files"],
        "decision": result["decision"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
