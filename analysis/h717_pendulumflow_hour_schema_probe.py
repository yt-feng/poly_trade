"""Cloud-only bounded schema probe for one PendulumFlow V3 hourly parquet.

The runner may download one manifest-selected hour, verify its declared size
and digest, and inspect Parquet metadata/schema. It never retains raw rows in
the report and never imports strategy or execution code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any


MAX_BYTES = 256 * 1024 * 1024
OWNER_FIELDS = {
    "owner", "owner_id", "maker", "maker_id", "taker", "taker_id", "trader",
    "wallet", "wallet_address", "address", "order_hash", "transaction_hash", "tx_hash",
}
SETTLEMENT_FIELDS = {
    "filled", "fill", "fill_id", "order_filled", "settlement", "settled",
    "block_number", "block_timestamp", "chain_id", "transaction_hash", "tx_hash",
}
BOOK_FIELDS = {
    "market", "condition_id", "asset_id", "token_id", "price", "size", "bids",
    "asks", "ts", "timestamp", "sequence", "seq", "event_type",
}


def _fetch(url: str, *, max_bytes: int) -> tuple[int, bytes]:
    import requests

    with requests.get(url, stream=True, timeout=(10, 90), headers={"User-Agent": "poly-trade-h717-hour-probe/1"}) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("CONTENT_LENGTH_EXCEEDS_BOUND")
        total = 0
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("RESPONSE_EXCEEDS_BOUND")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)


def _download(url: str, path: Path, *, expected_bytes: int, max_bytes: int) -> tuple[int, str, int]:
    import requests

    if expected_bytes <= 0 or expected_bytes > max_bytes:
        raise ValueError("MANIFEST_BYTES_OUT_OF_BOUND")
    digest = hashlib.sha256()
    total = 0
    with requests.get(url, stream=True, timeout=(10, 180), headers={"User-Agent": "poly-trade-h717-hour-probe/1"}) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("CONTENT_LENGTH_EXCEEDS_BOUND")
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("DOWNLOAD_EXCEEDS_BOUND")
                digest.update(chunk)
                handle.write(chunk)
    return total, digest.hexdigest(), response.status_code


def _field_flags(names: set[str]) -> dict[str, Any]:
    lowered = {name.lower() for name in names}
    owner = sorted(lowered & OWNER_FIELDS)
    settlement = sorted(lowered & SETTLEMENT_FIELDS)
    book = sorted(lowered & BOOK_FIELDS)
    return {
        "book_fields_present": book,
        "owner_identity_fields_present": owner,
        "settlement_fields_present": settlement,
        "owner_identity_proven": bool(owner),
        "settlement_identity_proven": bool(settlement),
    }


def probe(base: str, hour: str, *, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    import pyarrow.parquet as pq

    prefix = f"{base.rstrip('/')}/v3/{hour}"
    manifest_url = f"{prefix}/manifest.json"
    parquet_url = f"https://dl.pendulumflow.com/v3/{hour}/{hour.replace('/', 'T')}.parquet"
    report: dict[str, Any] = {
        "format": "h717-pendulumflow-hour-schema-probe-v1",
        "status": "complete",
        "hour": hour,
        "target_date_files_read": True,
        "parquet_bytes_downloaded": 0,
        "strategy_evaluated": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "manifest_url": manifest_url,
        "parquet_url": parquet_url,
    }
    try:
        manifest_status, manifest_body = _fetch(manifest_url, max_bytes=256 * 1024)
        report["manifest_status"] = manifest_status
        if manifest_status != 200:
            raise RuntimeError("MANIFEST_HTTP_ERROR")
        manifest = json.loads(manifest_body)
        expected_bytes = manifest.get("bytes")
        expected_sha = manifest.get("sha256")
        report.update({
            "manifest_row_count": manifest.get("row_count") if type(manifest.get("row_count")) is int else None,
            "manifest_bytes": expected_bytes if type(expected_bytes) is int else None,
            "manifest_sha256_present": isinstance(expected_sha, str) and len(expected_sha) == 64,
            "layout": manifest.get("layout") if isinstance(manifest.get("layout"), str) else None,
            "served_verbatim": manifest.get("served_verbatim") is True,
        })
        if type(expected_bytes) is not int or type(expected_sha) is not str:
            raise ValueError("MANIFEST_INTEGRITY_FIELDS_MISSING")
        with tempfile.TemporaryDirectory(prefix="h717-pendulumflow-hour-") as directory:
            path = Path(directory) / "hour.parquet"
            downloaded, actual_sha, parquet_status = _download(
                parquet_url, path, expected_bytes=expected_bytes, max_bytes=max_bytes
            )
            report.update({
                "parquet_status": parquet_status,
                "parquet_bytes_downloaded": downloaded,
                "download_size_matches_manifest": downloaded == expected_bytes,
                "sha256_matches_manifest": actual_sha == expected_sha,
            })
            if downloaded != expected_bytes or actual_sha != expected_sha:
                raise ValueError("ARCHIVE_INTEGRITY_MISMATCH")
            parquet = pq.ParquetFile(path)
            names = set(parquet.schema_arrow.names)
            report.update({
                "parquet_row_groups": parquet.num_row_groups,
                "parquet_rows": parquet.metadata.num_rows,
                "schema_names": sorted(names),
                "schema_name_count": len(names),
                **_field_flags(names),
            })
    except Exception as exc:  # bounded diagnostic is sealed by the workflow
        report["status"] = "incomplete"
        report["error_type"] = type(exc).__name__
        report["decision"] = "wait_or_retry_once_after_service_rate_window"
        return report
    if report.get("owner_identity_proven") and report.get("settlement_identity_proven"):
        report["decision"] = "schema_identity_ready_for_separate_chain_join_no_strategy_admission"
    else:
        report["decision"] = "public_book_or_trade_schema_only_owner_settlement_unproven_no_h508_admission"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://archive.pendulumflow.com")
    parser.add_argument("--hour", default="2026-09-03/17")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    args = parser.parse_args()
    report = probe(args.base, args.hour, max_bytes=args.max_bytes)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "decision", "parquet_bytes_downloaded")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
