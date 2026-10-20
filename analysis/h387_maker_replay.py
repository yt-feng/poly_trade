"""Run the H387 public complete-set maker replay through one $10 cash ledger.

The upstream replay reports per-window market PnL.  This adapter turns those
rows into a chronological cash ledger: a $5 split is debited at entry and
``$5 + pnl`` is returned at the upstream redeem time.  Rows are never reset,
concatenated, or selected independently.  This is paper evidence only.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


INITIAL_CASH = Decimal("10")
SPLIT_SIZE = Decimal("5")
REDEEM_DELAY_SECONDS = 366
TEN_ROWS = 10


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid_decimal") from exc
    if not result.is_finite():
        raise ValueError("non_finite_decimal")
    return result


def _queue_key(raw: str) -> str:
    # The upstream JSON key is ``label,queue=<value>``.  Keep it opaque in the
    # report while normalising whitespace so a duplicate key cannot create a
    # second ledger.
    return ",".join(part.strip() for part in raw.split(","))


def replay_series(rows: list[dict[str, Any]], *, split_size: Decimal = SPLIT_SIZE) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: int(row["start"]))
    cash = INITIAL_CASH
    pending: list[tuple[int, Decimal]] = []
    ledger_rows: list[dict[str, Any]] = []
    skipped_insufficient = 0
    for row in ordered:
        start = int(row["start"])
        # Release only after the recorded redeem time.  This preserves the
        # overlap/cash constraint between adjacent five-minute windows.
        released = Decimal("0")
        still_pending: list[tuple[int, Decimal]] = []
        for release_ts, return_cash in pending:
            if release_ts <= start:
                cash += return_cash
                released += return_cash
            else:
                still_pending.append((release_ts, return_cash))
        pending = still_pending
        pnl = _decimal(row.get("pnl", "0"))
        if cash < split_size:
            skipped_insufficient += 1
            ledger_rows.append({
                "start": start,
                "eligible": False,
                "cash_before": str(cash),
                "released": str(released),
                "pnl": str(pnl),
                "reason": "insufficient_free_cash",
            })
            continue
        cash_before = cash
        cash -= split_size
        return_cash = split_size + pnl
        pending.append((start + REDEEM_DELAY_SECONDS, return_cash))
        ledger_rows.append({
            "start": start,
            "eligible": True,
            "cash_before": str(cash_before),
            "cash_after_entry": str(cash),
            "released": str(released),
            "pnl": str(pnl),
            "return_at_redeem": str(return_cash),
            "release_ts": start + REDEEM_DELAY_SECONDS,
        })
    for _, return_cash in sorted(pending):
        cash += return_cash
    eligible = [row for row in ledger_rows if row["eligible"]]
    ten = eligible[:TEN_ROWS]
    ten_row_chain = len(ten) == TEN_ROWS
    ten_min_cash = min((_decimal(row["cash_before"]) for row in ten), default=Decimal("0"))
    return {
        "initial_cash": str(INITIAL_CASH),
        "split_size": str(split_size),
        "redeem_delay_seconds": REDEEM_DELAY_SECONDS,
        "input_rows": len(ordered),
        "eligible_rows": len(eligible),
        "skipped_insufficient_cash": skipped_insufficient,
        "same_ledger_rows": len(eligible),
        "first_ten_consecutive_rows": len(ten),
        "ten_row_same_ledger_gate": ten_row_chain,
        "first_ten_min_cash_before_entry": str(ten_min_cash),
        "ending_cash_after_pending_release": str(cash),
        "paper_chain_passed": ten_row_chain and ten_min_cash >= split_size,
        "ledger_reset_count": 0,
        "concatenation_count": 0,
        "ledger_rows": ledger_rows,
    }


def build_report(payload: dict[str, Any], *, source_commit: str, protocol: str) -> dict[str, Any]:
    per_window = payload.get("per_window")
    if not isinstance(per_window, dict):
        raise ValueError("missing_per_window")
    series = {}
    for raw_key, rows in sorted(per_window.items()):
        if not isinstance(rows, list):
            raise ValueError("invalid_series")
        series[_queue_key(str(raw_key))] = replay_series(rows)
    return {
        "format": "h387-complete-set-maker-replay-v1",
        "source": {
            "repository": "0xbrutusdev/polymarket-5min-crypto-trading-bot",
            "commit": source_commit,
            "protocol": protocol,
            "input_windows": len(payload.get("windows", [])),
        },
        "paper_only": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "cash_ledger_protocol": {
            "initial_cash": str(INITIAL_CASH),
            "fixed_split_size": str(SPLIT_SIZE),
            "minimum_free_cash": str(SPLIT_SIZE),
            "continuous": True,
            "resets_allowed": False,
            "concatenation_allowed": False,
            "ten_rows_required": True,
        },
        "series": series,
        "decision": "dormant_paper_evidence_only",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--protocol", default="post_only_paired_complete_set")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = build_report(payload, source_commit=args.source_commit, protocol=args.protocol)
    Path(args.output).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
