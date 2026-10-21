"""Build a privacy-preserving identity index for the fixed H391 raw tape.

The H391 raw rows contain the identity and amount fields needed for a future
owner/order join, but the replay adapter intentionally drops them.  This
module writes a deterministic gzip JSONL sidecar containing only SHA-256
digests for identity/hash/asset fields and the numeric execution fields.  It
does not replay a strategy, collect a new market period, or emit raw values.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Iterable

RAW_FIELDS = (
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
DIGEST_FIELDS = (
    "id",
    "maker",
    "taker",
    "orderHash",
    "transactionHash",
    "makerAssetId",
    "takerAssetId",
)
NUMERIC_FIELDS = ("makerAmountFilled", "takerAmountFilled", "timestamp", "fee")


def _digest(value: Any) -> str:
    """Return a stable, domain-separated digest without retaining ``value``."""
    if value in (None, ""):
        return ""
    return "sha256:" + hashlib.sha256(("h462-v1\x00" + str(value)).encode("utf-8")).hexdigest()


def _iter_rows(fills_dir: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    files = sorted(fills_dir.glob("*.fills.jsonl.gz"))
    if not files:
        raise ValueError("no_fill_files")
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row_not_object")
                yield path.name, row


def build_sidecar(
    fills_dir: Path,
    *,
    source_commit: str,
    dataset_revision: str,
    output: Path,
) -> dict[str, Any]:
    """Write the redacted sidecar and return an aggregate-only report."""
    output.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    complete = 0
    missing = {field: 0 for field in RAW_FIELDS}
    file_counts: dict[str, int] = {}
    # Fix the gzip timestamp so the encrypted payload has a reproducible digest.
    with output.open("wb") as raw_output:
        with gzip.GzipFile(fileobj=raw_output, filename="", mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as handle:
                for file_name, row in _iter_rows(fills_dir):
                    total += 1
                    file_counts[file_name] = file_counts.get(file_name, 0) + 1
                    absent = [field for field in RAW_FIELDS if row.get(field) in (None, "")]
                    if not absent:
                        complete += 1
                    for field in absent:
                        missing[field] += 1
                    redacted = {
                        "source_file": file_name,
                        "identity_digest": {field: _digest(row.get(field)) for field in DIGEST_FIELDS},
                        "numeric": {field: str(row.get(field, "")) for field in NUMERIC_FIELDS},
                    }
                    handle.write(json.dumps(redacted, sort_keys=True, separators=(",", ":")) + "\n")
    sidecar_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    report: dict[str, Any] = {
        "format": "h462-h391-identity-sidecar-v1",
        "source": {
            "source_commit": source_commit,
            "dataset_revision": dataset_revision,
            "file_count": len(file_counts),
        },
        "rows": {
            "total": total,
            "complete_identity_rows": complete,
            "complete_identity_fraction": complete / total if total else 0.0,
        },
        "missing_raw_fields": {field: count for field, count in missing.items() if count},
        "sidecar": {
            "path": output.name,
            "sha256": sidecar_sha256,
            "compressed_bytes": output.stat().st_size,
            "rows": total,
        },
        "redaction": {
            "digest_fields": list(DIGEST_FIELDS),
            "numeric_fields": list(NUMERIC_FIELDS),
            "raw_values_emitted": False,
            "raw_addresses_hashes_asset_ids_emitted": False,
        },
        "strategy_evaluated": False,
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "privacy": "identity and hash fields are domain-separated SHA-256 digests; numeric fields are retained for a private join only",
        "file_counts": dict(sorted(file_counts.items())),
    }
    report["report_sha256"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = build_sidecar(
        args.fills_dir,
        source_commit=args.source_commit,
        dataset_revision=args.dataset_revision,
        output=args.sidecar,
    )
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": report["format"], "rows": report["rows"], "sidecar": report["sidecar"]}, sort_keys=True))


if __name__ == "__main__":
    main()
