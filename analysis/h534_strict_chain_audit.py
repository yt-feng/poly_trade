"""Strict fixed-fraction ten-trade chain audit for the H534 paper result.

The original H534 grid records each rolling-window replay but leaves
``same_ledger_compounding_rows`` at zero as a placeholder.  This audit does
not change the signal, fraction, source or stress grid.  It derives the
already-recorded fixed 0.55 rows into an explicit, independently checked
selection/holdout chain count and keeps execution evidence separate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_FORMAT = "old-clean-h534-dense-cash-fraction-grid-v1"
EXPECTED_HYPOTHESIS = "H534_DENSE_FIXED_CASH_FRACTION_GRID"
FIXED_FRACTION = 0.55
SELECTION_WINDOWS = 8
REQUIRED_FILLS = 10
INITIAL_CASH = 10.0
MINIMUM_ORDER_USD = 5.0


def _number(value: Any) -> float:
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError("NONFINITE_NUMBER")
    return result


def _bool(value: Any) -> bool:
    return value is True


def _row_passes(row: dict[str, Any]) -> bool:
    """Require a complete, same-ledger ten-row prefix, not just a positive prefix."""
    return (
        int(row.get("first_ten_fills", -1)) == REQUIRED_FILLS
        and _bool(row.get("first_ten_passed"))
        and _number(row.get("first_ten_min_cash_before_entry")) >= MINIMUM_ORDER_USD
        and _number(row.get("first_ten_ending_cash")) >= INITIAL_CASH
    )


def audit(summary_path: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("SUMMARY_OBJECT_REQUIRED")
    if summary.get("format") != EXPECTED_FORMAT:
        raise ValueError("H534_FORMAT_MISMATCH")
    if summary.get("hypothesis_id") != EXPECTED_HYPOTHESIS:
        raise ValueError("H534_HYPOTHESIS_MISMATCH")
    if summary.get("source_commit") != source_commit or summary.get("observed_source_commit") != source_commit:
        raise ValueError("SOURCE_COMMIT_MISMATCH")
    protocol = summary.get("protocol")
    if not isinstance(protocol, dict) or not (
        _number(protocol.get("initial_cash_usd")) == INITIAL_CASH
        and int(protocol.get("required_fills")) == REQUIRED_FILLS
        and _number(protocol.get("minimum_order_usd")) == MINIMUM_ORDER_USD
        and protocol.get("same_ledger_within_window") is True
        and protocol.get("no_cross_window_concatenation") is True
        and protocol.get("old_clean_dataset_only") is True
        and protocol.get("paper_only") is True
    ):
        raise ValueError("H534_PROTOCOL_MISMATCH")
    rows = summary.get("window_rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("H534_WINDOW_ROWS_REQUIRED")
    fixed = [row for row in rows if isinstance(row, dict) and _number(row.get("gross_cash_fraction")) == FIXED_FRACTION]
    if len(fixed) < REQUIRED_FILLS:
        raise ValueError("FIXED_FRACTION_ROWS_INCOMPLETE")
    identities: set[tuple[float, float, float, int]] = set()
    for row in fixed:
        identity = (
            _number(row.get("slippage")),
            _number(row.get("latency_seconds")),
            _number(row.get("depth_haircut")),
            int(row.get("window_index")),
        )
        if identity in identities:
            raise ValueError("DUPLICATE_FIXED_FRACTION_ROW")
        identities.add(identity)
    selection = [row for row in fixed if int(row["window_index"]) <= SELECTION_WINDOWS]
    holdout = [row for row in fixed if int(row["window_index"]) > SELECTION_WINDOWS]
    if not selection or not holdout:
        raise ValueError("SELECTION_HOLDOUT_REQUIRED")
    passing = [row for row in fixed if _row_passes(row)]
    selection_passing = [row for row in selection if _row_passes(row)]
    holdout_passing = [row for row in holdout if _row_passes(row)]
    stress_cells = sorted({
        (_number(row["slippage"]), _number(row["latency_seconds"]), _number(row["depth_haircut"]))
        for row in fixed
    })
    expected_windows = sorted({int(row["window_index"]) for row in fixed})
    expected_rows = len(stress_cells) * len(expected_windows)
    if len(fixed) != expected_rows:
        raise ValueError("FIXED_FRACTION_GRID_INCOMPLETE")
    result = {
        "format": "old-clean-h534-strict-chain-audit-v1",
        "hypothesis_id": "H534R_STRICT_FIXED_055_CHAIN_AUDIT",
        "parent_hypothesis_id": EXPECTED_HYPOTHESIS,
        "source_commit": source_commit,
        "parent_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "fixed_fraction": FIXED_FRACTION,
        "selection_windows": SELECTION_WINDOWS,
        "holdout_windows": len(expected_windows) - SELECTION_WINDOWS,
        "stress_cells": len(stress_cells),
        "fixed_fraction_rows": len(fixed),
        "same_ledger_compounding_rows": len(passing),
        "selection": {
            "rows": len(selection),
            "passing_rows": len(selection_passing),
            "all_ten": bool(selection) and len(selection_passing) == len(selection),
            "min_fills": min((int(row["first_ten_fills"]) for row in selection), default=0),
            "min_cash": min((_number(row["first_ten_ending_cash"]) for row in selection), default=INITIAL_CASH),
        },
        "holdout": {
            "rows": len(holdout),
            "passing_rows": len(holdout_passing),
            "all_ten": bool(holdout) and len(holdout_passing) == len(holdout),
            "min_fills": min((int(row["first_ten_fills"]) for row in holdout), default=0),
            "min_cash": min((_number(row["first_ten_ending_cash"]) for row in holdout), default=INITIAL_CASH),
        },
        "strict_chain_gate": bool(passing) and len(selection_passing) == len(selection) and len(holdout_passing) == len(holdout),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "evidence_boundary": "re-derived same-ledger paper chains from H534 recorded rows; no authenticated orders, settlement or balance receipts",
        "next_action": "retain only if strict chain gate and owner-scoped execution evidence are separately satisfied; never treat paper rows as canary fills",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.summary, source_commit=args.source_commit, output=args.output)
    print(json.dumps({
        "format": result["format"],
        "same_ledger_compounding_rows": result["same_ledger_compounding_rows"],
        "strict_chain_gate": result["strict_chain_gate"],
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
