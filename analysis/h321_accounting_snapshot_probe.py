"""Bounded read-only probe for Polymarket's accounting snapshot export.

The current official SDK exposes ``GET /v1/accounting/snapshot?user=...``.
This probe records only response shape, hashes, field names and bounded row
counts.  It never publishes owner addresses, raw accounting rows, credentials,
or response bodies in plaintext.  The result is a source-availability audit;
it does not admit a fill, settlement, cash release, or Canary row.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
from urllib.error import HTTPError
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable


BASE_URL = "https://data-api.polymarket.com"
MAX_ACCOUNTS = 4
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_TABLE_BYTES = 8 * 1024 * 1024
MAX_ROWS_PER_TABLE = 100_000
OWNER_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BTC5M_RE = re.compile(r"btc[^\n]{0,80}(?:up|down)[^\n]{0,80}5m|btc[^\n]{0,80}5m", re.I)
TX_RE = re.compile(r"(?:tx|transaction|hash)", re.I)
CASH_RE = re.compile(r"(?:cash|collateral|usdc|deposit|withdraw|transfer|payout|redeem)", re.I)
SETTLEMENT_RE = re.compile(r"(?:settle|resolution|redeem|payout|winner|closed)", re.I)


def _failure_code(error: BaseException) -> str:
    """Return a bounded provider category without retaining response content."""
    if isinstance(error, HTTPError):
        code = int(getattr(error, "code", 0) or 0)
        if 100 <= code <= 599:
            return f"http_{code}"
        return "http_error"
    return type(error).__name__


def _owner(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not OWNER_RE.fullmatch(text):
        raise ValueError("account_owner_required")
    return text


def _row_text(row: dict[str, Any]) -> str:
    return " ".join(str(value) for value in row.values() if value is not None)


def _rows_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value[:MAX_ROWS_PER_TABLE] if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "rows", "items", "records", "activity", "trades", "positions"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate[:MAX_ROWS_PER_TABLE] if isinstance(item, dict)]
        return [value]
    return []


def _summarize_rows(name: str, rows: list[dict[str, Any]], *, bytes_read: int) -> dict[str, Any]:
    columns = sorted({str(key) for row in rows for key in row})
    text_hits = sum(bool(BTC5M_RE.search(_row_text(row))) for row in rows)
    tx_hits = sum(any(TX_RE.search(str(key)) for key in row) for row in rows)
    cash_hits = sum(any(CASH_RE.search(str(key)) for key in row) for row in rows)
    settlement_hits = sum(any(SETTLEMENT_RE.search(str(key)) for key in row) for row in rows)
    return {
        "name": name,
        "bytes_read": bytes_read,
        "rows_seen": len(rows),
        "columns": columns[:300],
        "btc_five_minute_row_count": text_hits,
        "rows_with_transaction_like_field": tx_hits,
        "rows_with_cash_like_field": cash_hits,
        "rows_with_settlement_like_field": settlement_hits,
    }


def _parse_table(name: str, data: bytes) -> dict[str, Any]:
    body = data[:MAX_TABLE_BYTES]
    suffix = name.lower()
    try:
        if suffix.endswith((".json", ".jsonl")):
            if suffix.endswith(".jsonl"):
                rows = []
                for line in body.decode("utf-8", errors="replace").splitlines()[:MAX_ROWS_PER_TABLE]:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
            else:
                rows = _rows_from_json(json.loads(body.decode("utf-8")))
            return _summarize_rows(name, rows, bytes_read=len(body))
        if suffix.endswith((".csv", ".tsv")):
            delimiter = "\t" if suffix.endswith(".tsv") else ","
            reader = csv.DictReader(io.StringIO(body.decode("utf-8", errors="replace")), delimiter=delimiter)
            rows = [dict(row) for _, row in zip(range(MAX_ROWS_PER_TABLE), reader)]
            return _summarize_rows(name, rows, bytes_read=len(body))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        return {"name": name, "bytes_read": len(body), "parse_error": type(error).__name__}
    return {"name": name, "bytes_read": len(body), "parse_error": "unsupported_table_format"}


def summarize_response(data: bytes, content_type: str = "") -> dict[str, Any]:
    """Summarize an accounting response without retaining row values."""
    if not isinstance(data, bytes):
        raise TypeError("response_bytes_required")
    if len(data) > MAX_RESPONSE_BYTES:
        return {
            "byte_length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": content_type,
            "response_too_large": True,
            "archive_entries": [],
            "tables": [],
        }
    result = {
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": content_type,
        "response_too_large": False,
        "archive_entries": [],
        "tables": [],
    }
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = []
            for info in archive.infolist()[:200]:
                entries.append({"name": info.filename, "file_size": info.file_size})
                if not info.is_dir() and info.file_size <= MAX_TABLE_BYTES:
                    lower = info.filename.lower()
                    if lower.endswith((".csv", ".tsv", ".json", ".jsonl")):
                        result["tables"].append(_parse_table(info.filename, archive.read(info)))
            result["archive_entries"] = entries
        return result
    if data[:2] == b"\x1f\x8b":
        import gzip
        inflated = gzip.decompress(data)
        return {**result, "tables": [_parse_table("response.jsonl", inflated)]}
    content_lower = content_type.lower()
    if "json" in content_lower or data.lstrip()[:1] in (b"[", b"{"):
        result["tables"] = [_parse_table("response.json", data)]
    elif "csv" in content_lower or "text" in content_lower:
        result["tables"] = [_parse_table("response.csv", data)]
    else:
        result["tables"] = [_parse_table("response", data)]
    return result


def _default_fetch(url: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream,application/json,text/csv"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return int(response.status), dict(response.headers.items()), response.read(MAX_RESPONSE_BYTES + 1)


def probe_accounts(
    accounts: Iterable[dict[str, Any]],
    *,
    fetcher: Callable[[str], tuple[int, dict[str, str], bytes]] = _default_fetch,
    base_url: str = BASE_URL,
) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    reports = []
    for account in rows:
        owner = _owner(account.get("owner"))
        query = urllib.parse.urlencode({"user": owner})
        url = base_url.rstrip("/") + "/v1/accounting/snapshot?" + query
        owner_hash = hashlib.sha256(owner.encode("ascii")).hexdigest()
        try:
            status, headers, data = fetcher(url)
            summary = summarize_response(data, headers.get("Content-Type", headers.get("content-type", "")))
            reports.append({
                "label": str(account.get("label") or ""),
                "owner_address_sha256": owner_hash,
                "http_status": status,
                "response": summary,
            })
        except Exception as error:
            reports.append({
                "label": str(account.get("label") or ""),
                "owner_address_sha256": owner_hash,
                "http_status": None,
                "failure_code": _failure_code(error),
            })
    all_tables = [table for report in reports for table in report.get("response", {}).get("tables", [])]
    return {
        "format": "h321-accounting-snapshot-probe-v1",
        "endpoint": "/v1/accounting/snapshot",
        "scope": "bounded read-only accounting snapshot source availability; shape and field coverage only",
        "accounts": reports,
        "account_count": len(reports),
        "successful_accounts": sum(report.get("http_status") == 200 for report in reports),
        "table_count": len(all_tables),
        "btc_five_minute_rows": sum(table.get("btc_five_minute_row_count", 0) for table in all_tables),
        "transaction_like_rows": sum(table.get("rows_with_transaction_like_field", 0) for table in all_tables),
        "cash_like_rows": sum(table.get("rows_with_cash_like_field", 0) for table in all_tables),
        "settlement_like_rows": sum(table.get("rows_with_settlement_like_field", 0) for table in all_tables),
        "owner_scoped_bundle_available": False,
        "owner_scoped_full_settlement_cash_bundle_available": False,
        "balance_continuity_proven": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "strategy_evaluated": False,
        "new_strategy_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    accounts = json.loads(args.input.read_text())
    result = probe_accounts(accounts, base_url=args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("account_count", "successful_accounts", "table_count", "btc_five_minute_rows", "owner_scoped_bundle_available")}))


if __name__ == "__main__":
    main()
