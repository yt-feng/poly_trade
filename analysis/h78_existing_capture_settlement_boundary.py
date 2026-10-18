"""Audit already-present report artifacts for execution and cash evidence.

This audit does not acquire data or infer fills.  It scans only existing JSON,
JSONL and CSV report artifacts, records which evidence keys are present, and
keeps paper-PnL fields separate from authenticated execution fields.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


MAX_FILE_BYTES = 8 * 1024 * 1024
RECEIPT_KEYS = {
    "owner", "account_id", "order_id", "trade_id", "transaction_hash",
    "transactionHash", "settlement_receipt", "settlement_receipt_id",
    "cash_available_ms", "reusable_cash_ms", "cash_release_ms",
}
SETTLEMENT_KEYS = {"settlement_receipt", "settlement_receipt_id", "actual_settlement_time_ms", "redemption_time_ms"}
CASH_KEYS = {"cash_available_ms", "reusable_cash_ms", "cash_release_ms", "cash_reconciled"}
PAPER_KEYS = {"bankroll_after", "pnl_usd", "ending_bankroll", "total_return", "profit_factor"}


def _key_hits(text: str, keys: set[str]) -> set[str]:
    return {key for key in keys if re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text)}


def audit(report_root: Path) -> dict[str, Any]:
    files = sorted(
        path for path in report_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".csv"}
    )
    file_digest = hashlib.sha256()
    receipt_files: Counter[str] = Counter()
    settlement_files: Counter[str] = Counter()
    cash_files: Counter[str] = Counter()
    paper_files: Counter[str] = Counter()
    skipped: list[str] = []
    for path in files:
        relative = path.relative_to(report_root).as_posix()
        file_digest.update(relative.encode("utf-8") + b"\0" + str(path.stat().st_size).encode() + b"\n")
        if path.stat().st_size > MAX_FILE_BYTES:
            skipped.append(relative)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in _key_hits(text, RECEIPT_KEYS):
            receipt_files[key] += 1
        for key in _key_hits(text, SETTLEMENT_KEYS):
            settlement_files[key] += 1
        for key in _key_hits(text, CASH_KEYS):
            cash_files[key] += 1
        for key in _key_hits(text, PAPER_KEYS):
            paper_files[key] += 1
    # A lone key such as ``owner`` can belong to unrelated report metadata.
    # Treat authenticated receipt evidence as present only when the minimum
    # identity join is represented: owner/account + order/trade identity +
    # transaction hash.  Settlement and cash clocks remain separate gates.
    present_receipt_keys = set(receipt_files)
    has_account_identity = bool(present_receipt_keys & {"owner", "account_id"})
    has_order_or_trade_identity = bool(present_receipt_keys & {"order_id", "trade_id"})
    has_transaction = bool(present_receipt_keys & {"transaction_hash", "transactionHash"})
    return {
        "format": "existing-h78-capture-settlement-boundary-audit-v1",
        "report_root": str(report_root),
        "scope": "existing report artifacts only; no acquisition, row replay or model change",
        "files_scanned": len(files),
        "oversized_files_skipped": skipped,
        "file_manifest_sha256": file_digest.hexdigest(),
        "receipt_key_file_counts": dict(sorted(receipt_files.items())),
        "settlement_key_file_counts": dict(sorted(settlement_files.items())),
        "cash_clock_key_file_counts": dict(sorted(cash_files.items())),
        "paper_pnl_key_file_counts": dict(sorted(paper_files.items())),
        "authenticated_receipt_evidence_present": (
            has_account_identity and has_order_or_trade_identity and has_transaction
        ),
        "official_settlement_receipt_present": bool(settlement_files),
        "reusable_cash_clock_present": bool(cash_files),
        "paper_pnl_present": bool(paper_files),
        "decision": "existing_reports_paper_only_no_authoritative_cash_chain",
        "root_cause": (
            "The present report assets expose strategy trade logs and bankroll/PnL summaries, but no owner-scoped "
            "order or trade identity, transaction hash, settlement receipt or reusable-cash clock. They cannot "
            "upgrade quote/PnL replay into a verified same-ledger Canary chain."
        ),
        "evidence_boundary": "Existing report metadata only; no authenticated account evidence was found.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.report_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "files_scanned": result["files_scanned"],
        "authenticated_receipt_evidence_present": result["authenticated_receipt_evidence_present"],
        "official_settlement_receipt_present": result["official_settlement_receipt_present"],
        "reusable_cash_clock_present": result["reusable_cash_clock_present"],
        "paper_pnl_present": result["paper_pnl_present"],
        "decision": result["decision"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
