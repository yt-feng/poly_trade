"""Audit already-produced old-clean metrics under one compound cash gate.

This is a read-only frontier synthesis.  It consumes CSV artifacts that were
already produced by prior old-clean studies; it does not download data, fit a
model, tune a threshold, or concatenate windows.  A cell is only a candidate
frontier row when it has five chronological holdout blocks and every block
contains the same-ledger first-ten contract under the fixed fee, minimum-order
and cash-release assumptions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


FEE_RATE = 0.07
RELEASE_SECONDS = 600.0
SETTLEMENT_SECONDS = 300.0
MIN_ORDER = 5.0
MIN_SHARES = 5.0
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
BLOCKS = tuple(f"holdout_block_{i}" for i in range(1, 6))


def _same(value: Any, expected: float) -> bool:
    try:
        return abs(float(value) - expected) <= 1e-12
    except (TypeError, ValueError):
        return False


def _filter_contract(frame: pd.DataFrame) -> pd.DataFrame:
    required = (
        "split", "slippage", "cash_release_delay_seconds", "fee_mode",
        "fee_rate", "min_order_notional", "min_order_shares",
        "settlement_seconds", "first_ten_fills", "first_ten_minimum_cash_usd",
        "first_ten_passed",
    )
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError("MISSING_COLUMNS:" + ",".join(missing))
    out = frame.copy()
    out = out[out["fee_mode"].astype(str).eq("polymarket_taker")]
    for column, expected in (
        ("fee_rate", FEE_RATE),
        ("cash_release_delay_seconds", RELEASE_SECONDS),
        ("settlement_seconds", SETTLEMENT_SECONDS),
        ("min_order_notional", MIN_ORDER),
        ("min_order_shares", MIN_SHARES),
    ):
        out = out[out[column].map(lambda value: _same(value, expected))]
        if out.empty:
            return out
    out = out[out["slippage"].map(lambda value: any(_same(value, item) for item in SLIPPAGES))]
    if out.empty:
        return out
    if "depth_floor" in out:
        out = out[out["depth_floor"].map(lambda value: any(_same(value, item) for item in DEPTH_FLOORS))]
    return out


def _candidate_label(frame: pd.DataFrame) -> pd.Series:
    if "candidate" in frame:
        return frame["candidate"].astype(str)
    if "strategy" in frame:
        return frame["strategy"].astype(str)
    return pd.Series(["unknown"] * len(frame), index=frame.index)


def _supplemental_json(path: Path) -> list[dict[str, Any]]:
    """Summarize known JSON artifacts whose execution contract differs.

    They are reported separately and never enter the fixed-contract frontier.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format") != "old-clean-h211-microprice-stream-v1":
        return [{"artifact": str(path), "protocol_compatible": False, "reason": "unsupported_json_artifact"}]
    rows: list[dict[str, Any]] = []
    for record in document.get("records") or []:
        blocks = record.get("holdout_blocks") or []
        prefixes = [block.get("prefix") or {} for block in blocks]
        if not prefixes:
            continue
        fills = [int(prefix.get("fills", 0)) for prefix in prefixes]
        cash = [float(prefix.get("minimum_cash", 0)) for prefix in prefixes]
        passed = [prefix.get("passed") is True for prefix in prefixes]
        rows.append({
            "artifact": str(path),
            "candidate": "h211_microprice",
            "parameter_cell": {
                "threshold": record.get("threshold"),
                "price_cap": record.get("price_cap"),
                "require_path_efficiency": record.get("require_path_efficiency"),
            },
            "protocol_compatible": False,
            "protocol_reason": "cash_release_seconds=900_not_frozen_600",
            "block_count": len(prefixes),
            "worst_block_first_ten_fills": min(fills),
            "worst_block_first_ten_minimum_cash_usd": min(cash),
            "all_block_first_ten_passed": all(passed),
            "paper_chain_fields_available": True,
            "worst_block_full_chain_fills": min(fills),
            "worst_block_full_chain_minimum_cash_usd": min(cash),
            "all_block_full_chain_passed": all(passed),
            "admission_passed": False,
        })
    return rows


def audit(paths: Iterable[Path], *, json_paths: Iterable[Path] = ()) -> dict[str, Any]:
    frontier: list[dict[str, Any]] = []
    supplemental: list[dict[str, Any]] = []
    artifact_rows = 0
    contract_rows = 0
    files_read = 0
    files_skipped: list[dict[str, str]] = []
    for path in paths:
        files_read += 1
        try:
            frame = pd.read_csv(path)
        except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            files_skipped.append({"artifact": str(path), "reason": f"READ_ERROR:{type(exc).__name__}"})
            continue
        artifact_rows += len(frame)
        try:
            filtered = _filter_contract(frame)
        except ValueError as exc:
            # Older reports intentionally used a smaller schema.  Preserve
            # them in the audit record without treating missing diagnostics as
            # an admissible result or aborting the other independent files.
            files_skipped.append({"artifact": str(path), "reason": str(exc)})
            continue
        contract_rows += len(filtered)
        if filtered.empty:
            continue
        filtered = filtered.copy()
        filtered["_candidate"] = _candidate_label(filtered)
        parameter_columns = [column for column in ("slippage", "depth_floor", "fraction", "capacity_ratio") if column in filtered]
        for values, group in filtered.groupby(["_candidate", *parameter_columns], dropna=False, sort=False):
            if not isinstance(values, tuple):
                values = (values,)
            candidate = str(values[0])
            block = group[group["split"].astype(str).isin(BLOCKS)]
            blocks_seen = sorted(set(block["split"].astype(str)))
            if block.empty:
                continue
            row: dict[str, Any] = {
                "artifact": str(path),
                "candidate": candidate,
                "parameter_cell": {column: values[index + 1] for index, column in enumerate(parameter_columns)},
                "block_count": len(blocks_seen),
                "blocks_seen": blocks_seen,
                "worst_block_first_ten_fills": int(pd.to_numeric(block["first_ten_fills"], errors="coerce").min()),
                "worst_block_first_ten_minimum_cash_usd": float(pd.to_numeric(block["first_ten_minimum_cash_usd"], errors="coerce").min()),
                "all_block_first_ten_passed": bool(block["first_ten_passed"].astype(bool).all()),
            }
            has_full_chain = all(column in block.columns for column in (
                "paper_chain_fills", "paper_chain_minimum_cash_usd", "paper_chain_passed"
            ))
            row["paper_chain_fields_available"] = has_full_chain
            row["worst_block_full_chain_fills"] = (
                int(pd.to_numeric(block["paper_chain_fills"], errors="coerce").min())
                if has_full_chain else None
            )
            row["worst_block_full_chain_minimum_cash_usd"] = (
                float(pd.to_numeric(block["paper_chain_minimum_cash_usd"], errors="coerce").min())
                if has_full_chain else None
            )
            row["all_block_full_chain_passed"] = (
                bool(block["paper_chain_passed"].astype(bool).all())
                if has_full_chain else False
            )
            row["admission_passed"] = bool(
                row["block_count"] == len(BLOCKS)
                and row["worst_block_first_ten_fills"] >= 10
                and row["worst_block_first_ten_minimum_cash_usd"] >= MIN_ORDER
                and row["paper_chain_fields_available"]
                and row["worst_block_full_chain_fills"] >= 10
                and row["worst_block_full_chain_minimum_cash_usd"] >= MIN_ORDER
                and row["all_block_first_ten_passed"]
                and row["all_block_full_chain_passed"]
            )
            frontier.append(row)

    for path in json_paths:
        supplemental.extend(_supplemental_json(path))

    frontier.sort(
        key=lambda row: (
            row["admission_passed"],
            row["worst_block_first_ten_fills"],
            row["worst_block_first_ten_minimum_cash_usd"],
            row["worst_block_full_chain_minimum_cash_usd"]
            if row["worst_block_full_chain_minimum_cash_usd"] is not None else float("-inf"),
        ),
        reverse=True,
    )
    admitted = [row for row in frontier if row["admission_passed"]]
    return {
        "format": "h335-existing-old-clean-frontier-audit-v1",
        "hypothesis_id": "H335-existing-old-clean-frontier-audit",
        "source_scope": "existing old-clean metric CSV artifacts only; no new data, refit or parameter search",
        "protocol": {
            "initial_cash_usd": 10.0,
            "required_fills": 10,
            "same_evolving_cash_ledger": True,
            "no_window_concatenation": True,
            "fee_rate": FEE_RATE,
            "cash_release_seconds": RELEASE_SECONDS,
            "settlement_seconds": SETTLEMENT_SECONDS,
            "minimum_order_notional_usd": MIN_ORDER,
            "minimum_order_shares": MIN_SHARES,
            "chronological_holdout_blocks": list(BLOCKS),
        },
        "files_read": files_read,
        "files_skipped": files_skipped,
        "artifact_rows": artifact_rows,
        "contract_rows": contract_rows,
        "frontier_rows": frontier,
        "frontier_row_count": len(frontier),
        "supplemental_out_of_contract_rows": supplemental,
        "supplemental_out_of_contract_row_count": len(supplemental),
        "admitted_candidates": admitted,
        "decision": "existing_frontier_has_no_admissible_continuous_chain" if not admitted else "existing_frontier_contains_admissible_chain_requiring_receipt_gate",
        "root_cause": "Previously produced pooled or single-holdout prefixes do not establish a five-block continuous cash chain. This audit prevents an overlooked artifact from being promoted while keeping all artifacts separated by source and parameter cell.",
        "strategy_evaluated": True,
        "paper_only": True,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "next_action": "Keep live buying paused; only a genuinely independent owner-scoped current-v2 settlement and reusable-balance chain can open the Canary gate.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--json-input", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.input, json_paths=args.json_input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("decision", "files_read", "contract_rows", "frontier_row_count", "actual_fills", "verified_canary_fills")}, sort_keys=True))


if __name__ == "__main__":
    main()
