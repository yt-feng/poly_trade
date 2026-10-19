"""Diagnose the fixed-contract capital floor in existing old-clean metrics.

This is a read-only synthesis of already-produced CSV artifacts.  It does not
fit, tune, download, concatenate windows, or promote a strategy.  The goal is
to separate a lack of ten chronological fills from the stricter requirement
that the same evolving cash ledger keeps at least one minimum order available
after fees, settlement, and the cash-release delay.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from analysis.h335_existing_frontier_audit import _filter_contract


MIN_ORDER = 5.0


def _read_contract_rows(paths: Iterable[Path]) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    frames: list[pd.DataFrame] = []
    skipped: list[dict[str, str]] = []
    for path in paths:
        try:
            frame = pd.read_csv(path)
            filtered = _filter_contract(frame)
        except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            skipped.append({"artifact": str(path), "reason": f"READ_ERROR:{type(exc).__name__}"})
            continue
        except ValueError as exc:
            skipped.append({"artifact": str(path), "reason": str(exc)})
            continue
        if filtered.empty:
            continue
        filtered = filtered.copy()
        filtered["artifact"] = str(path)
        frames.append(filtered)
    if not frames:
        return pd.DataFrame(), skipped
    return pd.concat(frames, ignore_index=True), skipped


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes"}


def diagnose(paths: Iterable[Path]) -> dict[str, Any]:
    frame, skipped = _read_contract_rows(paths)
    if frame.empty:
        raise ValueError("NO_FIXED_CONTRACT_ROWS")

    first_fills = pd.to_numeric(frame["first_ten_fills"], errors="coerce")
    first_cash = pd.to_numeric(frame["first_ten_minimum_cash_usd"], errors="coerce")
    paper_fills = pd.to_numeric(frame["paper_chain_fills"], errors="coerce")
    paper_cash = pd.to_numeric(frame["paper_chain_minimum_cash_usd"], errors="coerce")
    first_ten_fill_rows = first_fills.ge(10)
    paper_fill_rows = paper_fills.ge(10)
    first_ten_cash_rows = first_ten_fill_rows & first_cash.ge(MIN_ORDER)
    paper_cash_rows = paper_fill_rows & paper_cash.ge(MIN_ORDER)

    def max_record(fill_series: pd.Series, cash_series: pd.Series) -> dict[str, Any]:
        eligible = fill_series.notna()
        if not eligible.any():
            return {}
        index = fill_series[eligible].idxmax()
        row = frame.loc[index]
        return {
            "candidate": str(row.get("candidate", row.get("strategy", "unknown"))),
            "split": str(row.get("split", "unknown")),
            "fills": int(fill_series.loc[index]),
            "minimum_cash_usd": float(cash_series.loc[index]),
            "cash_shortfall_to_minimum_order_usd": max(0.0, MIN_ORDER - float(cash_series.loc[index])),
            "artifact": str(row["artifact"]),
        }

    first_failure = Counter(
        str(value) for value in frame.loc[~_bool_series(frame["first_ten_passed"]), "first_ten_failure_reason"]
        if str(value) != "nan"
    )
    paper_failure = Counter(
        str(value) for value in frame.loc[~_bool_series(frame["paper_chain_passed"]), "paper_chain_failure_reason"]
        if str(value) != "nan"
    )
    return {
        "format": "h337-old-clean-capital-floor-diagnostic-v1",
        "hypothesis_id": "H337-old-clean-capital-floor-diagnostic",
        "source_scope": "existing old-clean metric CSV artifacts under the frozen 600-second contract; no new data, refit or parameter search",
        "protocol": {
            "initial_cash_usd": 10.0,
            "required_fills": 10,
            "same_evolving_cash_ledger": True,
            "minimum_order_notional_usd": MIN_ORDER,
            "cash_release_seconds": 600.0,
            "settlement_seconds": 300.0,
        },
        "files_read": len(set(frame["artifact"])),
        "files_skipped": skipped,
        "contract_rows": int(len(frame)),
        "first_ten_fill_rows": int(first_ten_fill_rows.sum()),
        "first_ten_fill_and_cash_floor_rows": int(first_ten_cash_rows.sum()),
        "paper_chain_fill_rows": int(paper_fill_rows.sum()),
        "paper_chain_fill_and_cash_floor_rows": int(paper_cash_rows.sum()),
        "first_ten_failure_reasons": dict(first_failure),
        "paper_chain_failure_reasons": dict(paper_failure),
        "best_first_ten_fill_row": max_record(first_fills, first_cash),
        "best_paper_chain_fill_row": max_record(paper_fills, paper_cash),
        "decision": "capital_floor_is_binding_before_ten_fill_admission" if first_ten_fill_rows.any() and not first_ten_cash_rows.any() else "capital_floor_not_isolated",
        "root_cause": "Existing rows can sometimes produce ten chronological entries, but none retain the frozen minimum-order cash floor on the same evolving ledger. This separates trade availability from executable capital continuity without promoting a paper result.",
        "strategy_evaluated": False,
        "paper_only": True,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "next_action": "Keep the ten-fill gate and $5 minimum cash floor frozen; only test a predeclared execution-economics repair that preserves owner settlement and reusable cash evidence.",
    }


def _bool_series(values: pd.Series) -> pd.Series:
    return values.map(_bool)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "decision", "contract_rows", "first_ten_fill_rows",
        "first_ten_fill_and_cash_floor_rows", "paper_chain_fill_rows",
        "paper_chain_fill_and_cash_floor_rows",
    )}, sort_keys=True))


if __name__ == "__main__":
    main()
