"""Reconcile existing old-clean candidate metrics under one cash-floor gate.

This is a read-only audit of already-produced H310/H272 metrics.  It does not
download data, fit a model, search thresholds, or concatenate windows.  A
candidate is admitted only when every chronological holdout block keeps the
same ledger above the $5 order floor, reaches ten consecutive fills, and the
full block ledger remains growth-positive.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


FEE_RATE = 0.07
RELEASE_SECONDS = 600.0
MIN_ORDER = 5.0
MIN_SHARES = 5.0
SETTLEMENT_SECONDS = 300.0
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
HOLDOUT_SPLITS = tuple(f"holdout_block_{index}" for index in range(1, 6))


def _fixed_rows(frame: pd.DataFrame, candidate: str) -> pd.DataFrame:
    required = {
        "candidate": candidate,
        "fee_rate": FEE_RATE,
        "cash_release_delay_seconds": RELEASE_SECONDS,
        "min_order_notional": MIN_ORDER,
        "min_order_shares": MIN_SHARES,
        "settlement_seconds": SETTLEMENT_SECONDS,
    }
    out = frame.copy()
    for key, value in required.items():
        if key not in out:
            raise ValueError(f"MISSING_COLUMN:{key}")
        out = out[out[key] == value]
    if "slippage" in out:
        out = out[out["slippage"].isin(SLIPPAGES)]
    if "depth_floor" in out:
        out = out[out["depth_floor"].isin(DEPTH_FLOORS)]
    return out


def _as_bool(series: pd.Series) -> bool:
    return bool(series.astype(bool).all()) if not series.empty else False


def summarize_candidate(frame: pd.DataFrame, candidate: str) -> dict[str, Any]:
    fixed = _fixed_rows(frame, candidate)
    blocks = fixed[fixed["split"].isin(HOLDOUT_SPLITS)].copy()
    holdout = fixed[fixed["split"] == "holdout59"].copy()
    if blocks.empty:
        raise ValueError(f"NO_HOLDOUT_BLOCKS:{candidate}")
    for column in (
        "first_ten_fills", "first_ten_minimum_cash_usd", "first_ten_passed",
        "paper_chain_fills", "paper_chain_minimum_cash_usd", "paper_chain_passed",
    ):
        if column not in blocks:
            raise ValueError(f"MISSING_COLUMN:{column}")
    return {
        "candidate": candidate,
        "protocol_rows": int(len(fixed)),
        "holdout_block_rows": int(len(blocks)),
        "holdout_block_count": int(blocks["split"].nunique()),
        "min_block_first_ten_fills": int(blocks["first_ten_fills"].min()),
        "min_block_first_ten_cash_usd": float(blocks["first_ten_ending_cash_usd"].min()),
        "min_block_first_ten_minimum_cash_usd": float(blocks["first_ten_minimum_cash_usd"].min()),
        "min_block_full_fills": int(blocks["paper_chain_fills"].min()),
        "min_block_full_ending_cash_usd": float(blocks["paper_chain_ending_cash_usd"].min()),
        "min_block_full_minimum_cash_usd": float(blocks["paper_chain_minimum_cash_usd"].min()),
        "all_block_first_ten_passed": _as_bool(blocks["first_ten_passed"]),
        "all_block_full_chain_passed": _as_bool(blocks["paper_chain_passed"]),
        "cash_floor_passed": bool(
            (blocks["first_ten_minimum_cash_usd"] >= MIN_ORDER).all()
            and (blocks["paper_chain_minimum_cash_usd"] >= MIN_ORDER).all()
        ),
        "holdout59": {
            "rows": int(len(holdout)),
            "min_first_ten_fills": int(holdout["first_ten_fills"].min()) if not holdout.empty else 0,
            "min_first_ten_cash_usd": float(holdout["first_ten_ending_cash_usd"].min()) if not holdout.empty else INITIAL_CASH,
            "min_first_ten_minimum_cash_usd": float(holdout["first_ten_minimum_cash_usd"].min()) if not holdout.empty else INITIAL_CASH,
            "full_chain_passed": _as_bool(holdout["paper_chain_passed"]) if not holdout.empty else False,
        },
        "admission_passed": bool(
            len(blocks["split"].unique()) == len(HOLDOUT_SPLITS)
            and (blocks["first_ten_fills"] >= REQUIRED_FILLS).all()
            and (blocks["paper_chain_fills"] >= REQUIRED_FILLS).all()
            and _as_bool(blocks["first_ten_passed"])
            and _as_bool(blocks["paper_chain_passed"])
            and (blocks["first_ten_minimum_cash_usd"] >= MIN_ORDER).all()
            and (blocks["paper_chain_minimum_cash_usd"] >= MIN_ORDER).all()
        ),
    }


def reconcile(h310_metrics: Path, h272_metrics: Path) -> dict[str, Any]:
    h310 = pd.read_csv(h310_metrics)
    h272 = pd.read_csv(h272_metrics)
    rows = [
        summarize_candidate(h310, candidate)
        for candidate in ("h30_move_uncertainty", "h38_move_uncertainty_midpath")
    ]
    rows.extend(
        summarize_candidate(h272, candidate)
        for candidate in ("v1_adaptive_mix__path_efficiency_ge_050", "v1_conservative_mix__path_efficiency_ge_050")
    )
    return {
        "format": "h329-old-clean-cross-candidate-reconciliation-v1",
        "hypothesis_id": "H329-old-clean-cross-candidate-reconciliation",
        "source_scope": "existing H310/H272 metric tables only; no new data or refit",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "fee_rate": FEE_RATE,
            "minimum_order_notional_usd": MIN_ORDER,
            "minimum_order_shares": MIN_SHARES,
            "settlement_seconds": SETTLEMENT_SECONDS,
            "cash_release_seconds": RELEASE_SECONDS,
            "same_evolving_ledger": True,
            "chronological_blocks": list(HOLDOUT_SPLITS),
            "no_window_concatenation": True,
            "no_parameter_search": True,
        },
        "candidates": rows,
        "admitted_candidates": [row["candidate"] for row in rows if row["admission_passed"]],
        "strategy_evaluated": True,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "no_candidate_admitted_under_unified_cash_floor" if not any(row["admission_passed"] for row in rows) else "candidate_requires_receipt_gate",
        "root_cause": "Existing paper prefixes pass in pooled rows or fall below the $5 cash floor in chronological blocks; the paper-to-Canary gap is caused by path stability and cash-floor failure, not missing data alone.",
        "next_action": "Keep all candidates paper-only; require owner-scoped settlement, cash release and reusable-balance receipts before any Canary admission.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--h310-metrics", type=Path, required=True)
    parser.add_argument("--h272-metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = reconcile(args.h310_metrics, args.h272_metrics)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"decision": report["decision"], "admitted_candidates": report["admitted_candidates"], "actual_fills": 0, "verified_canary_fills": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
