"""H373 old-clean maker touch sensitivity under the continuous cash ledger.

This is a predeclared sensitivity audit of the existing H345 maker upper bound.
It varies only the observed ask-crossing amount and the causal observation
delay.  The source snapshot, H30/H38 signals, $5 minimum notional, zero maker
fee, five-minute settlement, ten-minute cash-release delay, and one-position
ledger stay fixed.  A quote touch is an optimistic proxy and can never count
as a Canary fill.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h53_prefix_only_compound_audit import (
    CANDIDATES,
    INITIAL_CASH,
    MINIMUM_ORDER,
    add_frozen_candidates,
    chain_summary,
    paper_events,
)
from maker_path_efficiency_shadow import inject_maker_execution
from maker_shadow_chain import attach_maker_snapshots


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUNS = 20
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
DELAYS_SECONDS = (4, 30, 300)
CROSSING_CENTS = (0.0, 0.25, 0.5, 1.0)
DEPTH_FLOORS = (100.0, 150.0)
MAKER_FEE_RATE = 0.0
CASH_RELEASE_DELAY = 600.0
REQUIRED_FILLS = 10


def holdout_blocks(runs: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    if cursor != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_HOLDOUT_EXACTLY")
    return blocks


def delay_label(delay: float) -> str:
    return str(int(delay)) if float(delay).is_integer() else str(delay).replace(".", "p")


def maker_touch_mask(frame: pd.DataFrame, candidate: str, delay: float) -> pd.Series:
    side = frame[f"{candidate}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(frame[f"{candidate}_minute"], errors="coerce")
    label = delay_label(delay)
    touch = pd.Series(False, index=frame.index)
    for side_name in ("buy_up", "buy_down"):
        for entry_minute in (1, 2, 4):
            rows = side.eq(side_name) & minute.eq(entry_minute)
            column = f"maker_{side_name}_{entry_minute}m_d{label}_touch"
            touch.loc[rows] = pd.to_numeric(frame.loc[rows, column], errors="coerce").fillna(0).eq(1)
    return touch


def inject_delay_execution(frame: pd.DataFrame, candidate: str, delay: float) -> pd.DataFrame:
    """Map the selected maker snapshot to the common cash-lock schema."""
    out = frame.copy()
    side = out[f"{candidate}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(out[f"{candidate}_minute"], errors="coerce")
    label = delay_label(delay)
    for side_name in ("buy_up", "buy_down"):
        for entry_minute in (1, 2, 4):
            rows = side.eq(side_name) & minute.eq(entry_minute)
            source = f"maker_{side_name}_{entry_minute}m_d{label}"
            target = f"exec_{side_name}_{entry_minute}m_d{label}"
            for field in ("price", "size", "spread", "overround"):
                out.loc[rows, f"{target}_quote_{field}"] = pd.to_numeric(
                    out.loc[rows, f"{source}_{field}"], errors="coerce"
                )
            out.loc[rows, f"{target}_signal_price"] = pd.to_numeric(
                out.loc[rows, f"{source}_price"], errors="coerce"
            )
            out.loc[rows, f"{target}_adverse_slippage"] = 0.0
    return out


def prepare(source_root: Path) -> tuple[pd.DataFrame, list[str], pd.DataFrame]:
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = add_frozen_candidates(features)
    raw, _ = base.read_all_monthly_runs(source_root)
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    return features, runs, quotes


def replay(frame: pd.DataFrame, candidate: str, names: list[str], delay: float, depth: float) -> dict[str, Any]:
    execution = inject_delay_execution(frame, candidate, delay)
    logs, metrics = simulate_cash_locked(
        execution[execution["run_name"].astype(str).isin([str(name) for name in names])],
        candidate,
        study.ExecutionConfig(
            delay_seconds=int(delay),
            max_spread=0.04,
            max_overround=0.03,
            min_depth=float(depth),
            max_adverse_slippage=0.0,
            fixed_slippage=0.0,
        ),
        INITIAL_CASH,
        MINIMUM_ORDER,
        CASH_RELEASE_DELAY,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=MAKER_FEE_RATE,
        min_order_shares=5.0,
        single_position_only=True,
    )
    return {**metrics, **chain_summary(paper_events(logs))}


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, runs, quotes = prepare(source_root)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:SELECTION_RUNS], test_runs[SELECTION_RUNS:]
    splits = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits.extend((f"holdout_block_{i}", block) for i, block in enumerate(holdout_blocks(holdout_runs), 1))
    rows: list[dict[str, Any]] = []
    for crossing in CROSSING_CENTS:
        # Reuse the same feature columns for every crossing value; only the
        # touch label changes.  This keeps the sensitivity grid auditable.
        crossed = attach_maker_snapshots(
            features,
            quotes,
            delays=DELAYS_SECONDS,
            minutes=(1, 2, 4),
            crossing_cents=crossing,
        )
        for delay in DELAYS_SECONDS:
            for candidate in CANDIDATES:
                touch = maker_touch_mask(crossed, candidate, delay)
                gated = crossed.copy()
                gated.loc[~touch, [f"{candidate}_side", f"{candidate}_minute", f"{candidate}_fraction"]] = ["skip", -1, 0.0]
                for split, names in splits:
                    for depth in DEPTH_FLOORS:
                        metrics = replay(gated, candidate, names, delay, depth)
                        rows.append({
                            "candidate": candidate,
                            "crossing_cents": crossing,
                            "delay_seconds": delay,
                            "depth_floor": depth,
                            "split": split,
                            "maker_touch_rows": int(touch[gated["run_name"].astype(str).isin([str(n) for n in names])].sum()),
                            **metrics,
                        })
    table = pd.DataFrame(rows)
    selection = table[table["split"].eq("selection20")].copy()
    eligible = selection[
        (selection["first_ten_fills"] == REQUIRED_FILLS)
        & selection["first_ten_passed"].astype(bool)
        & (selection["first_ten_ending_cash_usd"] >= INITIAL_CASH)
    ]
    choices = (
        eligible.groupby(["candidate", "crossing_cents", "delay_seconds"], as_index=False)
        .agg(worst_selection_cash=("first_ten_ending_cash_usd", "min"), depth_cells=("depth_floor", "count"))
    )
    choices = choices[choices["depth_cells"] == len(DEPTH_FLOORS)].sort_values(
        ["worst_selection_cash", "candidate", "crossing_cents", "delay_seconds"], ascending=[False, True, True, True]
    )
    selected = None if choices.empty else choices.iloc[0].to_dict()
    selected_rows = table.iloc[0:0] if selected is None else table[
        (table["candidate"] == selected["candidate"])
        & (table["crossing_cents"] == selected["crossing_cents"])
        & (table["delay_seconds"] == selected["delay_seconds"])
    ]
    holdout = selected_rows[selected_rows["split"].eq("holdout59")]
    blocks = selected_rows[selected_rows["split"].str.startswith("holdout_block_")]
    holdout_passed = bool(
        len(holdout) == len(DEPTH_FLOORS)
        and (holdout["first_ten_fills"] == REQUIRED_FILLS).all()
        and holdout["first_ten_passed"].all()
        and (holdout["first_ten_ending_cash_usd"] >= INITIAL_CASH).all()
    )
    block_stability = bool(
        len(blocks) == len(HOLDOUT_BLOCK_SIZES) * len(DEPTH_FLOORS)
        and (blocks["first_ten_fills"] == REQUIRED_FILLS).all()
        and blocks["first_ten_passed"].all()
        and (blocks["first_ten_ending_cash_usd"] >= INITIAL_CASH).all()
    )
    report: dict[str, Any] = {
        "format": "old-clean-h373-maker-touch-sensitivity-v1",
        "hypothesis_id": "H373-maker-touch-sensitivity",
        "source_commit": source_commit,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(CANDIDATES),
        "grid": {"crossing_cents": list(CROSSING_CENTS), "delay_seconds": list(DELAYS_SECONDS), "depth_floors": list(DEPTH_FLOORS)},
        "protocol": {"initial_cash_usd": INITIAL_CASH, "minimum_order_usd": MINIMUM_ORDER, "minimum_order_shares": 5.0, "maker_fee_rate": MAKER_FEE_RATE, "settlement_seconds": 300.0, "cash_release_delay_seconds": CASH_RELEASE_DELAY, "same_ledger": True, "single_position_only": True, "required_fills": REQUIRED_FILLS, "paper_only": True},
        "selection_choice": selected,
        "holdout_passed": holdout_passed,
        "holdout_block_stability_passed": block_stability,
        "decision": "paper_maker_touch_candidate_survives_grid_but_receipt_gate_remains_closed" if holdout_passed and block_stability else "reject_no_stable_maker_touch_candidate",
        "root_cause": "A broader touch/latency sensitivity either remains sparse or fails the same-ledger cash-floor and block-stability gate; any surviving quote proxy is still not a fill.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "existing old-clean quote snapshots and outcome labels; no queue position, order acceptance, owner-scoped fill, settlement, or reusable-cash receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "metrics.csv", index=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("selection_choice", "holdout_passed", "holdout_block_stability_passed", "decision", "actual_fills")}, ensure_ascii=False, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
