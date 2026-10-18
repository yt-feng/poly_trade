"""H55: tick-size rounding and observed-depth stress for the ten-fill prefix.

H54 already covered share-unit, fee, slippage, and cash-release dimensions.
H55 keeps those choices frozen, applies conservative quote-price rounding to
predeclared tick sizes, and raises the observed entry-depth floor. It is an
old-clean paper replay only: no new data, no refit, and no live order.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Iterable

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain
from h54_execution_contract_prefix_sensitivity import (
    CANDIDATES,
    DOCS,
    FEE_RATE,
    HIGH_THRESHOLD,
    INITIAL_CASH,
    LOW_THRESHOLD,
    MINIMUM_ORDER_SHARES,
    SLIPPAGES,
    add_frozen_candidates,
    chronological_blocks,
    paper_events,
)

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
MINIMUM_ORDER_NOTIONAL = 0.0
CASH_RELEASE_DELAY_SECONDS = 600.0
TICK_SIZES = (0.005, 0.01)
DEPTH_FLOORS = (100.0, 150.0)
ENTRY_MINUTE = 2
FRACTION = 0.04
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def round_quote_prices(frame: pd.DataFrame, tick_size: float) -> pd.DataFrame:
    """Round executable quote prices upward, preserving missing values."""
    if tick_size <= 0 or not np.isfinite(tick_size):
        raise ValueError("TICK_SIZE_MUST_BE_POSITIVE")
    out = frame.copy()
    quote_columns = [column for column in out.columns if column.endswith("_quote_price")]
    if not quote_columns:
        raise RuntimeError("NO_EXECUTABLE_QUOTE_PRICE_COLUMNS")
    for column in quote_columns:
        values = pd.to_numeric(out[column], errors="coerce")
        rounded = np.ceil((values - 1e-12) / float(tick_size)) * float(tick_size)
        out[column] = rounded.where(values.notna(), np.nan)
    return out


def chain_summary(events: list[dict[str, str]]) -> dict[str, object]:
    kwargs = {
        "initial_cash": str(INITIAL_CASH),
        "minimum_order": "0.0000001",
        "required_fills": 10,
        "evidence_mode": "paper_upper_bound",
    }
    prefix = evaluate_compound_chain(events[:10], **kwargs)
    full = evaluate_compound_chain(events, **kwargs)
    return {
        "paper_chain_fills": int(full["fills"]),
        "paper_chain_ending_cash_usd": float(full["ending_cash"]),
        "paper_chain_passed": bool(full["paper_replay_passed"]),
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash_usd": float(prefix["ending_cash"]),
        "first_ten_minimum_cash_usd": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, candidate: str, names: list[str], tick_size: float, depth_floor: float, slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    rounded = round_quote_prices(frame, tick_size)
    logs, metrics = simulate_cash_locked(
        rounded[rounded["run_name"].astype(str).isin([str(name) for name in names])],
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
    return {**metrics, **chain_summary(paper_events(logs))}, logs


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)]
    splits.extend((f"holdout_block_{idx}", names) for idx, names in enumerate(chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES), start=1))
    rows: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for split, names in splits:
            configs = [(tick, depth, slip) for tick in TICK_SIZES for depth in DEPTH_FLOORS for slip in SLIPPAGES]
            if split.startswith("holdout_block_"):
                configs = [(TICK_SIZES[-1], DEPTH_FLOORS[-1], SLIPPAGES[0])]
            for tick_size, depth_floor, slippage in configs:
                metrics, _logs = replay(frame, candidate, names, tick_size, depth_floor, slippage)
                rows.append({"candidate": candidate, "split": split, "tick_size": tick_size, "depth_floor": depth_floor, "slippage": slippage, **metrics})
    result = pd.DataFrame(rows)
    gates: dict[str, dict[str, object]] = {}
    for candidate in CANDIDATES:
        candidate_rows = result[result["candidate"].eq(candidate)]
        primary = candidate_rows[candidate_rows["split"].isin(["selection20", "holdout59"])]
        selection_rows = primary[primary["split"].eq("selection20")]
        holdout_rows = primary[primary["split"].eq("holdout59")]
        selection_pass = bool(
            len(selection_rows) == len(TICK_SIZES) * len(DEPTH_FLOORS) * len(SLIPPAGES)
            and selection_rows["first_ten_passed"].all()
            and (selection_rows["first_ten_fills"] == 10).all()
        )
        holdout_pass = bool(
            len(holdout_rows) == len(TICK_SIZES) * len(DEPTH_FLOORS) * len(SLIPPAGES)
            and holdout_rows["first_ten_passed"].all()
            and (holdout_rows["first_ten_fills"] == 10).all()
        )
        gates[candidate] = {
            "selection_all_tick_depth_slippage_gate": selection_pass,
            "holdout_all_tick_depth_slippage_gate": holdout_pass,
            "exact_ten_tick_depth_gate": selection_pass and holdout_pass,
            "primary_rows": primary.to_dict(orient="records"),
            "holdout_block_stress_rows": candidate_rows[candidate_rows["split"].str.startswith("holdout_block_")].to_dict(orient="records"),
        }
    summary = {
        "format": "old-clean-h55-tick-depth-prefix-audit-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(CANDIDATES),
        "frozen_signal": {
            "score": "btc_move_2m * (1 - 2*abs(mid_up_prob_2m - 0.5))",
            "low_threshold": LOW_THRESHOLD,
            "high_threshold": HIGH_THRESHOLD,
            "entry_minute": ENTRY_MINUTE,
            "fraction": FRACTION,
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "tick_sizes": list(TICK_SIZES),
            "depth_floors": list(DEPTH_FLOORS),
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "required_fills": 10,
            "rounding_mode": "ceil executable quote price to tick before fixed slippage",
        },
        "documentation_sources": DOCS,
        "primary_target": "exactly ten chronological fills with tick rounding and observed depth; no reset or block concatenation",
        "acceptance": "all declared tick sizes, depth floors and slippages pass on selection20 and holdout59",
        "gates": gates,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"gates": {k: v["exact_ten_tick_depth_gate"] for k, v in gates.items()}}, ensure_ascii=False, sort_keys=True))
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
