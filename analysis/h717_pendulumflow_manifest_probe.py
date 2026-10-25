"""Cloud-only manifest probe for PendulumFlow V3 target-hour archives.

This checks dated public manifests and the browse-page contract without
downloading a parquet archive.  It is a source/ingestion audit, not a
strategy run: no market identifiers, order rows, wallets, or live clients are
loaded.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SHA256 = re.compile(r"^[0-9a-f]{64}$")
HOUR = re.compile(r"^2026-09-0[3-6]/17$")


def _get(url: str, *, max_bytes: int) -> tuple[int, bytes]:
    import requests

    with requests.get(url, stream=True, timeout=(10, 30), headers={"User-Agent": "poly-trade-h717-manifest-probe/1"}) as response:
        total = 0
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("RESPONSE_EXCEEDS_BOUND")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)


def _probe_hour(base: str, hour: str) -> dict[str, Any]:
    if not HOUR.fullmatch(hour):
        raise ValueError("UNEXPECTED_TARGET_HOUR")
    page_url = f"{base}/browse/v3/{hour}/"
    manifest_url = f"{base}/v3/{hour}/manifest.json"
    page_status, page_body = _get(page_url, max_bytes=2 * 1024 * 1024)
    manifest_status, manifest_body = _get(manifest_url, max_bytes=256 * 1024)
    entry: dict[str, Any] = {
        "hour": hour,
        "page_status": page_status,
        "manifest_status": manifest_status,
        "page_has_parquet_link": (f"/v3/{hour}/".encode() in page_body and b".parquet" in page_body),
    }
    if manifest_status != 200:
        entry.update({"manifest_compatible": False, "error_type": "ManifestHTTPError"})
        return entry
    manifest = json.loads(manifest_body)
    sha256 = manifest.get("sha256")
    checks = {
        "file_name": manifest.get("file") == f"2026-09-03T17.parquet" if hour == "2026-09-03/17" else isinstance(manifest.get("file"), str) and manifest["file"].endswith(".parquet"),
        "hour_utc": manifest.get("hour_utc", "").startswith(hour.replace("/", "T") + ":00:00"),
        "positive_rows": type(manifest.get("row_count")) is int and manifest["row_count"] > 0,
        "positive_bytes": type(manifest.get("bytes")) is int and manifest["bytes"] > 0,
        "sha256": isinstance(sha256, str) and bool(SHA256.fullmatch(sha256)),
        "event_sorted_union": manifest.get("layout") == "union-event-sorted-v1",
        "served_verbatim": manifest.get("served_verbatim") is True,
    }
    entry.update({
        "manifest_compatible": all(checks.values()),
        "checks": checks,
        "row_count": manifest.get("row_count") if type(manifest.get("row_count")) is int else None,
        "bytes": manifest.get("bytes") if type(manifest.get("bytes")) is int else None,
        "sha256_present": isinstance(sha256, str) and bool(SHA256.fullmatch(sha256)),
        "layout": manifest.get("layout") if isinstance(manifest.get("layout"), str) else None,
        "served_verbatim": manifest.get("served_verbatim") is True,
        "products": sorted(manifest.get("products", {}).keys()) if isinstance(manifest.get("products"), dict) else [],
    })
    return entry


def probe(base: str, hours: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format": "h717-pendulumflow-manifest-probe-v1",
        "status": "complete",
        "target_hours": hours,
        "target_date_files_read": False,
        "parquet_bytes_downloaded": 0,
        "strategy_evaluated": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "hours": [],
    }
    for hour in hours:
        try:
            result["hours"].append(_probe_hour(base, hour))
        except Exception as exc:  # bounded diagnostic stays sealed
            result["status"] = "failed"
            result["hours"].append({"hour": hour, "manifest_compatible": False, "error_type": type(exc).__name__})
    result["all_manifests_compatible"] = bool(result["hours"]) and all(
        item.get("manifest_compatible") is True and item.get("page_has_parquet_link") is True
        for item in result["hours"]
    )
    result["decision"] = (
        "public_dated_manifest_and_schema_contract_ready_for_one_cloud_adapter_but_execution_identity_still_missing"
        if result["status"] == "complete" and result["all_manifests_compatible"]
        else "do_not_dispatch_target_join"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://archive.pendulumflow.com")
    parser.add_argument("--hour", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = probe(args.base.rstrip("/"), args.hour)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "all_manifests_compatible", "decision")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
