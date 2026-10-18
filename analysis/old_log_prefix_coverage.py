"""Audit legacy old-data logs under the exact one-ledger ten-fill contract.

This is a coverage audit for logs that predate the formal selection/holdout
artifacts.  It never collects data or submits orders.  Logs with an observed
``fee_usd`` field are replayed with that fee.  Logs without it are replayed
only as an explicitly optimistic zero-fee lower bound and cannot be promoted.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .sequential_compound_gate import evaluate_compound_chain
except ImportError:  # pragma: no cover - direct script execution
    from sequential_compound_gate import evaluate_compound_chain


REQUIRED_COLUMNS = {
    "first_quote_ts",
    "filled_cost",
    "pnl_usd",
    "run_name",
    "fold_id",
    "market_id",
}
BLOCK_SIZES = (12, 12, 12, 12, 11)


def _candidate_column(frame: pd.DataFrame) -> str | None:
    for column in ("candidate", "hypothesis", "strategy"):
        if column in frame.columns:
            return column
    return None


def _events(frame: pd.DataFrame, fee_column: str | None) -> list[dict[str, Any]]:
    ordered = frame.sort_values(["entry_ts", "market_id"], kind="stable")
    events: list[dict[str, Any]] = []
    for row in ordered.itertuples(index=False):
        fee = getattr(row, "fee", 0.0) if fee_column else 0.0
        cost = float(row.cost)
        pnl = float(row.pnl)
        events.append(
            {
                "status": "paper",
                "entry_ts": float(row.entry_ts.timestamp()),
                # Legacy logs have no settlement timestamp.  Their frozen
                # execution contract is five-minute settlement plus 600s.
                "cash_available_ts": float(
                    (row.entry_ts + pd.Timedelta(seconds=900)).timestamp()
                ),
                "entry_cost": str(cost),
                "fee": str(float(fee)),
                "payout": str(cost + float(fee) + pnl),
            }
        )
    return events


def _prefix(frame: pd.DataFrame, fee_column: str | None) -> dict[str, Any]:
    result = evaluate_compound_chain(
        _events(frame, fee_column)[:10],
        initial_cash="10",
        minimum_order="5",
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        "fills": int(result["fills"]),
        "ending_cash": result["ending_cash"],
        "minimum_cash": result["minimum_cash"],
        "passed": bool(result["paper_replay_passed"]),
        "failure": result["failure_reason"],
    }


def _blocks(frame: pd.DataFrame, fee_column: str | None) -> list[dict[str, Any]]:
    folds = sorted(frame["fold_num"].unique())
    if len(folds) != sum((20, *BLOCK_SIZES)) - 20:
        return []
    result: list[dict[str, Any]] = []
    cursor = 0
    for block_index, size in enumerate(BLOCK_SIZES, start=1):
        selected = set(folds[cursor : cursor + size])
        cursor += size
        block = frame[frame["fold_num"].isin(selected)]
        prefix = _prefix(block, fee_column)
        result.append({"block": block_index, "folds": size, **prefix})
    return result


def audit(source_root: Path, output: Path, include_formal: bool = False) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    paths = sorted(source_root.rglob("trade_logs.csv"))
    for path in paths:
        relative = path.relative_to(source_root)
        if not include_formal and "formal" in relative.parts:
            continue
        try:
            frame = pd.read_csv(path)
        except Exception:
            skipped.append({"path": str(relative), "reason": "read_error"})
            continue
        missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
        if missing:
            skipped.append({"path": str(relative), "reason": "missing_columns", "columns": missing})
            continue
        candidate_column = _candidate_column(frame)
        if candidate_column is None:
            skipped.append({"path": str(relative), "reason": "no_candidate_column"})
            continue

        working = frame.copy()
        working["candidate_value"] = working[candidate_column].astype(str)
        working["fold_num"] = pd.to_numeric(working["fold_id"], errors="coerce")
        working["entry_ts"] = pd.to_datetime(working["first_quote_ts"], utc=True, errors="coerce")
        working["cost"] = pd.to_numeric(working["filled_cost"], errors="coerce")
        working["pnl"] = pd.to_numeric(working["pnl_usd"], errors="coerce")
        if "fee_usd" in working:
            working["fee"] = pd.to_numeric(working["fee_usd"], errors="coerce")
        working = working.dropna(subset=["fold_num", "entry_ts", "cost", "pnl"])
        working = working[working["cost"] >= 5]
        if "fee" in working:
            # A full loss has pnl == -(cost + fee), so the nonnegative payout
            # check must include the observed fee rather than drop that row.
            working = working[
                (working["fee"] >= 0)
                & (working["cost"] + working["fee"] + working["pnl"] >= 0)
            ]
            fee_modes = [("observed_fee", "fee")]
        else:
            working = working[working["cost"] + working["pnl"] >= 0]
            fee_modes = [("zero_fee_lower_bound", None)]

        for candidate, group in working.groupby("candidate_value", sort=True):
            group = group.sort_values(["entry_ts", "market_id"], kind="stable").drop_duplicates(
                "market_id", keep="first"
            )
            folds = sorted(group["fold_num"].unique())
            if len(folds) < 20:
                skipped.append({"path": str(relative), "candidate": candidate, "reason": "fewer_than_20_folds"})
                continue
            selection_folds = set(folds[:20])
            holdout_folds = set(folds[20:])
            selection = group[group["fold_num"].isin(selection_folds)]
            holdout = group[group["fold_num"].isin(holdout_folds)]
            for fee_mode, fee_column in fee_modes:
                selection_prefix = _prefix(selection, fee_column)
                holdout_prefix = _prefix(holdout, fee_column)
                blocks = _blocks(holdout, fee_column)
                records.append(
                    {
                        "path": str(relative),
                        "candidate": candidate,
                        "fee_mode": fee_mode,
                        "rows": int(len(group)),
                        "folds": int(len(folds)),
                        "selection_rows": int(len(selection)),
                        "holdout_rows": int(len(holdout)),
                        "selection": selection_prefix,
                        "holdout": holdout_prefix,
                        "holdout_blocks": blocks,
                        "same_ledger_both": bool(selection_prefix["passed"] and holdout_prefix["passed"]),
                        "block_gate": bool(blocks)
                        and all(block["passed"] for block in blocks),
                        "promotion_eligible": fee_mode == "observed_fee"
                        and bool(selection_prefix["passed"] and holdout_prefix["passed"]),
                    }
                )

    result = {
        "format": "legacy-old-log-prefix-coverage-v1",
        "source_root": str(source_root),
        "include_formal": bool(include_formal),
        "records": records,
        "skipped": skipped,
        "files_scanned": len({record["path"] for record in records}),
        "candidate_records": len(records),
        "same_ledger_records": sum(record["same_ledger_both"] for record in records),
        "promotion_eligible_records": sum(record["promotion_eligible"] for record in records),
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-formal", action="store_true")
    args = parser.parse_args()
    result = audit(args.source_root, args.output, args.include_formal)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "files_scanned",
                    "candidate_records",
                    "same_ledger_records",
                    "promotion_eligible_records",
                    "new_data_collected",
                    "model_refit",
                    "live_orders_enabled",
                )
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
