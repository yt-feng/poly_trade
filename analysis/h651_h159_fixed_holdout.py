"""H651: fixed holdout verification for the already selected H159 candidate.

H159 selected no candidate because its target gate required $1,000 in every
selection cost cell, so its holdout was never evaluated.  H651 does not retune
or select on holdout: it carries the predeclared selection-best
``classic_breakout_up__fraction_0p80`` unchanged into the five chronological
holdout blocks and reports the same one-ledger, fee, depth, slippage and cash
release contract.  This remains paper evidence only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h159_fraction_ladder_exact_target_chain as h159
import classic_min_order_chain as registry

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
FIXED_STRATEGY = "classic_breakout_up__fraction_0p80"
REQUIRED_FILLS = h159.REQUIRED_FILLS
INITIAL_CASH = h159.INITIAL_CASH
MINIMUM_ORDER_NOTIONAL = h159.MINIMUM_ORDER_NOTIONAL
SLIPPAGES = h159.SLIPPAGES
DEPTH_FLOORS = h159.DEPTH_FLOORS
HOLDOUT_BLOCK_SIZES = h159.HOLDOUT_BLOCK_SIZES


def _gate(rows: list[dict[str, Any]]) -> bool:
    """Require the existing H159 ten-fill, non-negative-growth gate.

    ``first_ten_minimum_cash_usd`` is retained as a diagnostic.  It is not a
    second minimum-order gate: the existing paper ledger already checks cash
    before each admitted entry, and the final post-tenth balance need not fund
    an eleventh entry.
    """
    return bool(rows) and all(
        bool(row.get("first_ten_passed"))
        and int(row.get("first_ten_fills", 0)) == REQUIRED_FILLS
        and float(row.get("first_ten_ending_cash_usd", 0.0)) >= INITIAL_CASH
        for row in rows
    )


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    frame, runs = registry.prepare(source_root, list(h159.CANDIDATES))
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame, variants = h159.add_fraction_variants(frame)
    if FIXED_STRATEGY not in variants:
        raise RuntimeError(f"FIXED_STRATEGY_NOT_REGISTERED:{FIXED_STRATEGY}")

    test_runs = runs[4:]
    holdout_runs = test_runs[20:]
    holdout_blocks = h159.chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    holdout_rows: list[dict[str, Any]] = []
    for slippage in SLIPPAGES:
        for depth_floor in DEPTH_FLOORS:
            holdout_rows.append(
                {
                    "split": "holdout59",
                    **h159.replay(frame, FIXED_STRATEGY, holdout_runs, slippage, depth_floor),
                }
            )

    block_rows: list[dict[str, Any]] = []
    for block_index, names in enumerate(holdout_blocks, start=1):
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                block_rows.append(
                    {
                        "split": f"holdout_block_{block_index}",
                        "block_index": block_index,
                        **h159.replay(frame, FIXED_STRATEGY, names, slippage, depth_floor),
                    }
                )

    holdout_gate = _gate(holdout_rows)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) * len(DEPTH_FLOORS) and _gate(block_rows)
    all_rows = holdout_rows + block_rows
    summary: dict[str, Any] = {
        "format": "old-clean-h651-h159-fixed-holdout-v1",
        "hypothesis_id": "H651_H159_FIXED_HIGH_GROWTH_HOLDOUT",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "fixed_strategy": FIXED_STRATEGY,
        "selection_origin": "H159 selection-best classic_breakout_up fraction 0.80; fixed before reading holdout",
        "holdout_rows": holdout_rows,
        "holdout_block_rows": block_rows,
        "holdout_ten_fill_gate": holdout_gate,
        "holdout_block_gate": block_gate,
        "min_holdout_first_ten_cash_usd": min((float(row["first_ten_ending_cash_usd"]) for row in all_rows), default=0.0),
        "median_holdout_first_ten_cash_usd": float(pd.Series([float(row["first_ten_ending_cash_usd"]) for row in holdout_rows]).median()) if holdout_rows else 0.0,
        "max_holdout_first_ten_cash_usd": max((float(row["first_ten_ending_cash_usd"]) for row in all_rows), default=0.0),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_consecutive_fills": REQUIRED_FILLS,
            "same_evolving_cash_ledger": True,
            "prior_cash_release_required": True,
            "no_reset": True,
            "no_cross_window_concatenation": True,
            "fee_rate": h159.FEE_RATE,
            "cash_release_delay_seconds": h159.CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "no_holdout_tuning": True,
            "paper_only": True,
        },
        "decision": (
            "retain_as_high_growth_paper_candidate_for_owner_bundle"
            if holdout_gate and block_gate
            else "reject_as_stable_high_growth_candidate"
        ),
        "root_cause": (
            "The fixed H159 high-growth candidate does not pass every chronological holdout block under the same fee/depth/cash contract."
            if not (holdout_gate and block_gate)
            else "The fixed H159 candidate survives the declared holdout blocks, but paper execution is still not an authenticated Canary."
        ),
        "evidence_boundary": "fixed existing-data holdout only; no refit, new market data, live order, funding or authenticated receipt",
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps({k: summary[k] for k in ("fixed_strategy", "holdout_ten_fill_gate", "holdout_block_gate", "decision")}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
