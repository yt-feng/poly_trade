"""H154: raw quote/depth sizing boundary for frozen H30/H38 signals.

This is a separate paper preflight.  It reuses the frozen H30/H38 signal
definitions and pinned old-clean source, changing only a predeclared sizing
fraction.  Every row is replayed on one chronological cash ledger with the
documented share floor, fee, depth, settlement and cash-release constraints.
No model is fit, no source data is collected, and no order is submitted.
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
    paper_events,
)
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
SIZING_FRACTIONS = (0.04, 0.08, 0.16, 0.32)
DEPTH_FLOORS = (100.0, 150.0)
CASH_RELEASE_DELAY_SECONDS = 600.0
REQUIRED_FILLS = 10
TARGET_CASH_USD = 1000.0


def _chain(events: list[dict[str, str]]) -> dict[str, object]:
    prefix = evaluate_compound_chain(
        events[:REQUIRED_FILLS],
        initial_cash=str(INITIAL_CASH),
        minimum_order="0.0000001",
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
    )
    endpoint = float(prefix["ending_cash"])
    return {
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash_usd": endpoint,
        "first_ten_minimum_cash_usd": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
        "target_growth_passed": bool(prefix["paper_replay_passed"] and endpoint >= TARGET_CASH_USD),
        "per_fill_geometric_multiplier": (endpoint / INITIAL_CASH) ** (1.0 / REQUIRED_FILLS)
        if endpoint > 0
        else 0.0,
    }


def replay(
    frame: pd.DataFrame,
    candidate: str,
    run_names: Iterable[str],
    fraction: float,
    slippage: float,
    depth_floor: float,
) -> dict[str, object]:
    if fraction not in SIZING_FRACTIONS:
        raise ValueError("UNDECLARED_SIZING_FRACTION")
    working = frame[frame["run_name"].astype(str).isin([str(x) for x in run_names])].copy()
    working[f"{candidate}_fraction"] = float(fraction)
    logs, metrics = simulate_cash_locked(
        working,
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage), min_depth=float(depth_floor)),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {
        "candidate": candidate,
        "fraction": float(fraction),
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        **metrics,
        **_chain(paper_events(logs)),
    }


def _group_passes(rows: list[dict[str, object]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS for row in rows
    )


def _select(rows: list[dict[str, object]]) -> dict[str, object] | None:
    eligible: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for fraction in SIZING_FRACTIONS:
            group = [row for row in rows if row["candidate"] == candidate and row["fraction"] == fraction]
            if not _group_passes(group):
                continue
            eligible.append(
                {
                    "candidate": candidate,
                    "fraction": float(fraction),
                    "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
                    "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
                }
            )
    return sorted(
        eligible,
        key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["candidate"], row["fraction"]),
    )[0] if eligible else None


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
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
            for fraction in SIZING_FRACTIONS:
                for slippage in SLIPPAGES:
                    for depth_floor in DEPTH_FLOORS:
                        rows.append({"split": split, **replay(frame, candidate, names, fraction, slippage, depth_floor)})
    selection_rows = [row for row in rows if row["split"] == "selection20"]
    holdout_rows = [row for row in rows if row["split"] == "holdout59"]
    choice = _select(selection_rows)
    chosen_holdout = [
        row for row in holdout_rows if choice and row["candidate"] == choice["candidate"] and row["fraction"] == choice["fraction"]
    ]
    holdout_pass = _group_passes(chosen_holdout)
    summary = {
        "format": "old-clean-h154-raw-quote-depth-sizing-replay-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "frozen_signal_candidates": list(CANDIDATES),
        "sizing_fractions": list(SIZING_FRACTIONS),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "target_cash_usd": TARGET_CASH_USD,
            "required_fills": REQUIRED_FILLS,
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
        "selection_rule": "select only a candidate/fraction whose every declared slippage and depth configuration passes ten chronological fills on selection20; rank by worst first-ten ending cash",
        "selection_choice": choice,
        "selection_eligible_count": sum(1 for candidate in CANDIDATES for fraction in SIZING_FRACTIONS if _group_passes([row for row in selection_rows if row["candidate"] == candidate and row["fraction"] == fraction])),
        "holdout_passed": holdout_pass,
        "holdout_target_growth_passed": bool(chosen_holdout) and all(bool(row["target_growth_passed"]) for row in chosen_holdout),
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
    print(json.dumps({k: summary[k] for k in ("selection_choice", "selection_eligible_count", "holdout_passed", "holdout_target_growth_passed")}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
