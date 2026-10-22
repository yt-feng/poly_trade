"""H220: predeclared usable-cash buffer on the frozen old-clean signals.

This audit changes only a causal execution admission rule: after the current
entry cost and taker fee are reserved, a declared amount of free cash must
remain available.  The buffer is evaluated from the current account state, not
from a future outcome or holdout label.  Signal definitions, source snapshot,
fee, depth, share floor, settlement delay and same-ledger ten-fill gate stay
frozen.  It is paper evidence only and never submits an order.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h54_execution_contract_prefix_sensitivity import (
    CANDIDATES,
    FEE_RATE,
    INITIAL_CASH,
    MINIMUM_ORDER_NOTIONAL,
    MINIMUM_ORDER_SHARES,
    SLIPPAGES,
    add_frozen_candidates,
    chain_summary,
    paper_events,
)

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
BUFFER_LEVELS = (0.0, 0.5, 1.0, 2.0)
DEPTH_FLOORS = (100.0, 150.0)
CASH_RELEASE_DELAY_SECONDS = 600.0
REQUIRED_FILLS = 10


def _group_passes(rows: list[dict[str, object]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS
        for row in rows
    )


def select_candidate(rows: list[dict[str, object]]) -> dict[str, object] | None:
    eligible: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for buffer in BUFFER_LEVELS:
            group = [
                row for row in rows
                if row["candidate"] == candidate and row["minimum_free_cash_after_entry"] == buffer
            ]
            if not _group_passes(group):
                continue
            eligible.append({
                "candidate": candidate,
                "minimum_free_cash_after_entry": float(buffer),
                "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
                "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
            })
    return sorted(
        eligible,
        key=lambda row: (
            -row["worst_first_ten_cash_usd"],
            -row["sum_first_ten_cash_usd"],
            row["candidate"],
            row["minimum_free_cash_after_entry"],
        ),
    )[0] if eligible else None


def replay(
    frame: pd.DataFrame,
    candidate: str,
    run_names: Iterable[str],
    buffer: float,
    slippage: float,
    depth_floor: float,
    minimum_order_notional: float = MINIMUM_ORDER_NOTIONAL,
) -> dict[str, object]:
    if not math.isfinite(float(minimum_order_notional)) or float(minimum_order_notional) < 0:
        raise ValueError("minimum_order_notional must be finite and non-negative")
    working = frame[frame["run_name"].astype(str).isin([str(x) for x in run_names])].copy()
    logs, metrics = simulate_cash_locked(
        working,
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage), min_depth=float(depth_floor)),
        INITIAL_CASH,
        float(minimum_order_notional),
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
        minimum_free_cash_after_entry=float(buffer),
    )
    return {
        "candidate": candidate,
        "minimum_free_cash_after_entry": float(buffer),
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        **metrics,
        **chain_summary(paper_events(logs)),
    }


def evaluate(
    source_root: Path,
    *,
    source_commit: str,
    output: Path,
    minimum_order_notional: float = MINIMUM_ORDER_NOTIONAL,
) -> dict[str, object]:
    if not math.isfinite(float(minimum_order_notional)) or float(minimum_order_notional) < 0:
        raise ValueError("minimum_order_notional must be finite and non-negative")
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    rows: list[dict[str, object]] = []
    for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs)):
        for candidate in CANDIDATES:
            for buffer in BUFFER_LEVELS:
                for slippage in SLIPPAGES:
                    for depth_floor in DEPTH_FLOORS:
                        rows.append({
                            "split": split,
                            **replay(
                                frame,
                                candidate,
                                names,
                                buffer,
                                slippage,
                                depth_floor,
                                minimum_order_notional=float(minimum_order_notional),
                            ),
                        })
    selection_rows = [row for row in rows if row["split"] == "selection20"]
    holdout_rows = [row for row in rows if row["split"] == "holdout59"]
    choice = select_candidate(selection_rows)
    chosen_holdout = [
        row for row in holdout_rows
        if choice
        and row["candidate"] == choice["candidate"]
        and row["minimum_free_cash_after_entry"] == choice["minimum_free_cash_after_entry"]
    ]
    holdout_pass = _group_passes(chosen_holdout)
    summary = {
        "format": "old-clean-h220-cash-buffer-replay-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(CANDIDATES),
        "buffer_levels": list(BUFFER_LEVELS),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": float(minimum_order_notional),
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "same_ledger": True,
            "no_concatenation": True,
            "no_refit": True,
            "paper_only": True,
        },
        "selection_rule": "Choose candidate/buffer only when every declared slippage and depth cell passes the same-ledger ten-fill prefix on selection20; rank by worst first-ten ending cash.",
        "selection_choice": choice,
        "selection_eligible_count": sum(
            1 for candidate in CANDIDATES for buffer in BUFFER_LEVELS
            if _group_passes([
                row for row in selection_rows
                if row["candidate"] == candidate and row["minimum_free_cash_after_entry"] == buffer
            ])
        ),
        "holdout_passed": holdout_pass,
        # The Canary target is evaluated at the tenth chronological fill.  A
        # separate full-chain field keeps later regime decay visible without
        # redefining the bounded ten-fill contract.
        "holdout_prefix_growth_passed": bool(chosen_holdout) and all(
            float(row["first_ten_ending_cash_usd"]) >= INITIAL_CASH for row in chosen_holdout
        ),
        "holdout_full_chain_growth_passed": bool(chosen_holdout) and all(
            float(row["ending_cash"]) >= INITIAL_CASH for row in chosen_holdout
        ),
        "rows": len(rows),
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean raw quote/depth paper replay; no authenticated order, fill, settlement or owner receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("selection_choice", "selection_eligible_count", "holdout_passed", "holdout_prefix_growth_passed", "holdout_full_chain_growth_passed")}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-order-notional", type=float, default=MINIMUM_ORDER_NOTIONAL)
    args = parser.parse_args()
    evaluate(
        args.source_root,
        source_commit=args.source_commit,
        output=args.output,
        minimum_order_notional=args.minimum_order_notional,
    )


if __name__ == "__main__":
    main()
