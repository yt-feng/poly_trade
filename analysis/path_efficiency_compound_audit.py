"""Audit the frozen path-efficiency candidate as one paper cash chain.

The input is an existing old-clean trade log produced by the causal
path-efficiency replay.  This module only filters and de-duplicates that
already-generated paper evidence; it does not collect data or place orders.
The authenticated Canary gate remains false because the input has no private
account receipt or settlement identity.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from sequential_compound_gate import evaluate_compound_chain


CANDIDATE = "v1_adaptive_mix__path_efficiency_ge_050"
REQUIRED_COLUMNS = {
    "candidate",
    "split",
    "strategy",
    "first_quote_ts",
    "release_ts",
    "market_id",
    "filled_cost",
    "fee_usd",
    "payout_at_settlement",
}


def paper_events_from_trade_log(
    trade_log: pd.DataFrame,
    *,
    candidate: str = CANDIDATE,
    split: str = "holdout59",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return deterministic paper events and row counts for one split."""
    missing = sorted(REQUIRED_COLUMNS - set(trade_log.columns))
    if missing:
        raise ValueError("missing_trade_log_columns:" + ",".join(missing))
    rows = trade_log[
        trade_log["candidate"].astype(str).eq(candidate)
        & trade_log["split"].astype(str).eq(split)
    ].copy()
    raw_rows = len(rows)
    if rows.empty:
        return [], {"raw_rows": 0, "unique_rows": 0, "duplicate_rows_removed": 0}
    rows["entry_dt"] = pd.to_datetime(rows["first_quote_ts"], utc=True, errors="coerce")
    rows["release_dt"] = pd.to_datetime(rows["release_ts"], utc=True, errors="coerce")
    if rows[["entry_dt", "release_dt"]].isna().any().any():
        raise ValueError("invalid_trade_log_timestamp")
    rows = rows.sort_values(["entry_dt", "market_id", "strategy"], kind="stable")
    rows = rows.drop_duplicates(
        subset=["entry_dt", "market_id", "strategy", "candidate"], keep="first"
    )
    events = [
        {
            "status": "paper",
            "entry_ts": float(row.entry_dt.timestamp()),
            "cash_available_ts": float(row.release_dt.timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in rows.itertuples()
    ]
    return events, {
        "raw_rows": int(raw_rows),
        "unique_rows": int(len(events)),
        "duplicate_rows_removed": int(raw_rows - len(events)),
    }


def audit_trade_log(
    path: Path,
    *,
    candidate: str = CANDIDATE,
    split: str = "holdout59",
    source_commit: str | None = None,
) -> dict[str, Any]:
    trade_log = pd.read_csv(path)
    events, counts = paper_events_from_trade_log(trade_log, candidate=candidate, split=split)
    chain = evaluate_compound_chain(
        events,
        initial_cash="10",
        minimum_order="5",
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    # Keep the user's exact gate visible even when the frozen holdout contains
    # more than ten rows: this is the first ten chronological events on the
    # same evolving cash ledger, never ten independently reset trials.
    prefix_chain = evaluate_compound_chain(
        events[:10],
        initial_cash="10",
        minimum_order="5",
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        "format": "old-clean-path-efficiency-paper-compound-audit-v1",
        "candidate": candidate,
        "split": split,
        "source_commit": source_commit,
        "source_scope": "existing old-clean quote/label trade log only",
        "row_counts": counts,
        "protocol": {
            "initial_cash": "10",
            "minimum_order_notional": "5",
            "required_fills": 10,
            "evidence_mode": "paper_upper_bound",
        },
        "compound_chain": chain,
        "required_prefix_chain": prefix_chain,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "decision": "freeze_for_authoritative_receipt_join",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--candidate", default=CANDIDATE)
    parser.add_argument("--split", default="holdout59")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    result = audit_trade_log(
        args.trade_log,
        candidate=args.candidate,
        split=args.split,
        source_commit=args.source_commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "candidate": result["candidate"],
        "split": result["split"],
        "unique_rows": result["row_counts"]["unique_rows"],
        "fills": result["compound_chain"]["fills"],
        "ending_cash": result["compound_chain"]["ending_cash"],
        "paper_replay_passed": result["compound_chain"]["paper_replay_passed"],
        "gate_passed": result["compound_chain"]["gate_passed"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
