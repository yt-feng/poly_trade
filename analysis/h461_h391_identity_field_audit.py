"""Audit execution-identity fields in the fixed H391 public tape.

The H391 adapter turns raw OrderFilled-shaped rows into the source engine's
``Print`` object and intentionally drops the raw identity fields.  This audit
checks the pinned, already-used tape without replaying a strategy and reports
only aggregate field coverage.  It never emits maker/taker addresses,
transaction hashes or order hashes.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


RAW_REQUIRED = (
    "id",
    "maker",
    "taker",
    "orderHash",
    "transactionHash",
    "makerAmountFilled",
    "takerAmountFilled",
    "makerAssetId",
    "takerAssetId",
    "timestamp",
    "fee",
)
MISSING_EXECUTION_FIELDS = (
    "queue_position",
    "order_acceptance_timestamp",
    "cancel_timestamp",
    "reward_accrual",
    "settlement_receipt",
    "cash_release_receipt",
)


def _row_shape(row: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(field for field in RAW_REQUIRED if field not in row or row[field] in (None, ""))
    return not missing, missing


def audit(fills_dir: Path, *, source_commit: str, dataset_revision: str) -> dict[str, Any]:
    files = sorted(fills_dir.glob("*.fills.jsonl.gz"))
    if not files:
        raise ValueError("no_fill_files")
    total = 0
    complete = 0
    missing_counts: Counter[str] = Counter()
    file_rows: list[dict[str, Any]] = []
    unique_ids: dict[str, set[str]] = {field: set() for field in ("id", "maker", "taker", "orderHash", "transactionHash")}
    duplicate_counts: Counter[str] = Counter()
    for path in files:
        rows = 0
        complete_rows = 0
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row_not_object")
                total += 1
                rows += 1
                ok, missing = _row_shape(row)
                if ok:
                    complete += 1
                    complete_rows += 1
                for field in missing:
                    missing_counts[field] += 1
                for field, values in unique_ids.items():
                    value = row.get(field)
                    if value not in (None, ""):
                        key = str(value)
                        if key in values:
                            duplicate_counts[field] += 1
                        values.add(key)
        file_rows.append({"file": path.name, "rows": rows, "complete_identity_rows": complete_rows})
    identity_fields = {
        field: {"present_rows": int(total - missing_counts[field]), "unique_values": len(values)}
        for field, values in unique_ids.items()
    }
    result: dict[str, Any] = {
        "format": "h461-h391-execution-identity-field-audit-v1",
        "source": {"source_commit": source_commit, "dataset_revision": dataset_revision, "file_count": len(files)},
        "rows": {"total": total, "complete_identity_rows": complete, "complete_identity_fraction": (complete / total if total else 0.0)},
        "identity_fields": identity_fields,
        "duplicate_raw_identity_rows": dict(sorted(duplicate_counts.items())),
        "missing_required_field_rows": dict(sorted(missing_counts.items())),
        "missing_for_causal_execution": list(MISSING_EXECUTION_FIELDS),
        "adapter_repair_target": "retain raw public execution identity fields in a private sidecar while keeping the source engine Print replay unchanged",
        "strategy_evaluated": False,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "privacy": "aggregate counts only; no maker/taker addresses, order hashes or transaction hashes are emitted",
        "file_rows": file_rows,
    }
    result["report_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.fills_dir, source_commit=args.source_commit, dataset_revision=args.dataset_revision)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "files": result["source"]["file_count"], "rows": result["rows"]}, sort_keys=True))


if __name__ == "__main__":
    main()
