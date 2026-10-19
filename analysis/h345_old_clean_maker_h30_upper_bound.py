"""H345: old-clean maker upper bound for the frozen H30/H38 signals.

This audit changes only the execution-side hypothesis.  It reuses the pinned
83-run clean snapshot, places a resting bid at the observed signal-time ask,
and accepts the deliberately strict historical touch proxy when a later ask
trades through that bid by at least one cent.  The cash ledger uses a zero-fee
maker surrogate and one pending position at a time so that the result tests
whether maker economics could remove the taker cash-floor obstruction.

The touch is only an optimistic quote proxy.  It is not order acceptance,
queue position, an authenticated fill, settlement, or reusable cash, and it
can never increment the Canary count.
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
DELAY_SECONDS = 600.0
DEPTH_FLOORS = (100.0, 150.0)
MAKER_FEE_RATE = 0.0
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


def maker_touch_mask(frame: pd.DataFrame, candidate: str) -> pd.Series:
    side = frame[f"{candidate}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(frame[f"{candidate}_minute"], errors="coerce")
    touch = pd.Series(False, index=frame.index)
    for side_name in ("buy_up", "buy_down"):
        for entry_minute in (1, 2, 4):
            rows = side.eq(side_name) & minute.eq(entry_minute)
            column = f"maker_{side_name}_{entry_minute}m_d4_touch"
            touch.loc[rows] = (
                pd.to_numeric(frame.loc[rows, column], errors="coerce")
                .fillna(0)
                .eq(1)
            )
    return touch


def prepare(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = add_frozen_candidates(features)
    raw, _ = base.read_all_monthly_runs(source_root)
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    return attach_maker_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4)), runs


def replay(frame: pd.DataFrame, candidate: str, names: list[str], depth: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        candidate,
        study.ExecutionConfig(
            delay_seconds=4,
            max_spread=0.04,
            max_overround=0.03,
            min_depth=float(depth),
            max_adverse_slippage=0.0,
            fixed_slippage=0.0,
        ),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=MAKER_FEE_RATE,
        min_order_shares=5.0,
        single_position_only=True,
    )
    return {
        **metrics,
        **chain_summary(paper_events(logs)),
    }


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, runs = prepare(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    splits: list[tuple[str, list[str]]] = [
        ("selection20", selection_runs),
        ("holdout59", holdout_runs),
    ]
    splits.extend(
        (f"holdout_block_{index}", block)
        for index, block in enumerate(holdout_blocks(holdout_runs), start=1)
    )

    rows: list[dict[str, Any]] = []
    touch_counts: dict[str, dict[str, int]] = {}
    for candidate in CANDIDATES:
        gated = features.copy()
        touch = maker_touch_mask(gated, candidate)
        touch_counts[candidate] = {}
        gated.loc[
            ~touch,
            [f"{candidate}_side", f"{candidate}_minute", f"{candidate}_fraction"],
        ] = ["skip", -1, 0.0]
        maker = inject_maker_execution(gated, candidate)
        for split, names in splits:
            split_frame = maker[maker["run_name"].astype(str).isin([str(name) for name in names])]
            touch_counts[candidate][split] = int(touch[split_frame.index].sum())
            for depth in DEPTH_FLOORS:
                rows.append(
                    {
                        "candidate": candidate,
                        "split": split,
                        "depth_floor": depth,
                        "maker_touch_rows": touch_counts[candidate][split],
                        **replay(maker, candidate, names, depth),
                    }
                )

    table = pd.DataFrame(rows)
    eligible: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        selection = table[(table["candidate"] == candidate) & (table["split"] == "selection20")]
        if len(selection) == len(DEPTH_FLOORS) and all(
            (selection["first_ten_fills"] == REQUIRED_FILLS)
            & selection["first_ten_passed"].astype(bool)
        ):
            eligible.append(
                {
                    "candidate": candidate,
                    "worst_first_ten_cash_usd": float(selection["first_ten_ending_cash_usd"].min()),
                }
            )
    selected = sorted(eligible, key=lambda row: (-row["worst_first_ten_cash_usd"], row["candidate"]))[0] if eligible else None
    holdout = table[(table["candidate"] == (selected or {}).get("candidate")) & (table["split"] == "holdout59")]
    blocks = table[(table["candidate"] == (selected or {}).get("candidate")) & table["split"].str.startswith("holdout_block_")]
    holdout_gate = bool(
        selected
        and len(holdout) == len(DEPTH_FLOORS)
        and (holdout["first_ten_fills"] == REQUIRED_FILLS).all()
        and holdout["first_ten_passed"].astype(bool).all()
        and (holdout["first_ten_ending_cash_usd"] >= INITIAL_CASH).all()
    )
    block_gate = bool(
        selected
        and len(blocks) == len(HOLDOUT_BLOCK_SIZES) * len(DEPTH_FLOORS)
        and (blocks["first_ten_fills"] == REQUIRED_FILLS).all()
        and blocks["first_ten_passed"].astype(bool).all()
        and (blocks["ending_cash"] >= MINIMUM_ORDER).all()
        and (blocks["net_return"] >= 0.0).all()
    )
    result: dict[str, Any] = {
        "format": "old-clean-h345-maker-h30-upper-bound-v1",
        "hypothesis_id": "H345",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(CANDIDATES),
        "selection_choice": selected,
        "selection_eligible_count": len(eligible),
        "touch_counts": touch_counts,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER,
            "minimum_order_shares": 5.0,
            "maker_fee_rate": MAKER_FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "single_position_only": True,
            "slippage": 0.0,
            "depth_floors": list(DEPTH_FLOORS),
            "required_fills": REQUIRED_FILLS,
            "same_ledger": True,
            "no_concatenation": True,
            "no_refit": True,
        },
        "holdout_prefix_gate": holdout_gate,
        "holdout_block_gate": block_gate,
        "gate_passed": bool(selected and holdout_gate and block_gate),
        "decision": "reject_maker_upper_bound_no_touch_rows" if not eligible else "retain_maker_upper_bound_for_receipt_design",
        "root_cause": "The strict one-cent resting-bid touch proxy is empty for both frozen H30 and H38 signals across selection, holdout and every holdout block. Zero maker fee therefore cannot rescue the existing path because the old quote archive has no observed maker-touch opportunities for these entries.",
        "evidence_boundary": "Optimistic historical quote-touch proxy; no queue position, order acceptance, authenticated fill, settlement, or reusable-cash receipt.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "metrics.csv", index=False)
    (output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
