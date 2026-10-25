"""Bounded cloud-only schema probe for Vike's no-key Polymarket sample.

This is an ingestion contract check, not a strategy run.  It downloads only
the public sample (bounded by ``--max-bytes``), inspects Parquet metadata and a
few rows, and never reads target-date archive files or account data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any


REQUIRED = {
    "book_events": {"token_id", "condition_id", "ts", "local_ts", "seq", "event_type", "bids", "asks"},
    "trades": {"token_id", "condition_id", "ts", "local_ts", "seq", "price", "size", "side"},
}


def _download(url: str, path: Path, max_bytes: int) -> int:
    import requests

    with requests.get(url, stream=True, timeout=(10, 60), headers={"User-Agent": "poly-trade-schema-probe/1"}) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("CONTENT_LENGTH_EXCEEDS_BOUND")
        total = 0
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("DOWNLOAD_EXCEEDS_BOUND")
                handle.write(chunk)
    return total


def probe(urls: dict[str, str], *, max_bytes: int) -> dict[str, Any]:
    import pyarrow.parquet as pq

    result: dict[str, Any] = {
        "format": "h717-vike-schema-probe-v1",
        "status": "complete",
        "sample_only": True,
        "target_date_files_read": False,
        "strategy_evaluated": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "streams": {},
    }
    with tempfile.TemporaryDirectory(prefix="h717-vike-probe-") as directory:
        for stream, url in urls.items():
            entry: dict[str, Any] = {"url": url}
            path = Path(directory) / f"{stream}.parquet"
            try:
                entry["downloaded_bytes"] = _download(url, path, max_bytes)
                parquet = pq.ParquetFile(path)
                names = set(parquet.schema_arrow.names)
                required = REQUIRED[stream]
                entry.update({
                    "row_groups": parquet.num_row_groups,
                    "rows": parquet.metadata.num_rows,
                    "schema_names": sorted(names),
                    "required_fields_present": sorted(required & names),
                    "required_fields_missing": sorted(required - names),
                    "schema_compatible": required <= names,
                })
                # Read at most four rows from the first row group; retain only
                # type/shape facts, never raw market or wallet identifiers.
                table = parquet.read_row_group(0, columns=sorted(required & names))
                entry["sample_rows_checked"] = min(4, table.num_rows)
                entry["sample_column_count"] = table.num_columns
            except Exception as exc:  # preserve a bounded diagnostic in the sealed report
                entry.update({"schema_compatible": False, "error_type": type(exc).__name__})
                result["status"] = "failed"
            result["streams"][stream] = entry
    result["all_required_streams_compatible"] = bool(
        result["streams"] and all(item.get("schema_compatible") is True for item in result["streams"].values())
    )
    result["decision"] = (
        "sample_contract_compatible_target_dates_still_require_authorized_export"
        if result["status"] == "complete" and result["all_required_streams_compatible"]
        else "do_not_dispatch_target_join"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-url", required=True)
    parser.add_argument("--trades-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    args = parser.parse_args()
    report = probe({"book_events": args.book_url, "trades": args.trades_url}, max_bytes=args.max_bytes)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "all_required_streams_compatible", "decision")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
