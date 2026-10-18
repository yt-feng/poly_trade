"""H247: H30 plus a pre-entry quote-continuity quality gate.

The H30 direction and thresholds stay frozen.  This experiment adds only
features visible before entry: quote count, side-specific spread, overround,
buy-side size and direction-aligned book pressure.  The quote-count floor is the median of the
selection20 signal rows and is frozen before any outcome or cash replay is
read.  No future labels, settlement fields or account receipts are used.

This is paper evidence on the same evolving cash ledger.  It is never a live
order path and cannot create authenticated Canary fills.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
import sys
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h54_execution_contract_prefix_sensitivity as h54
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
REQUIRED_FILLS = 10
MINIMUM_ORDER_NOTIONAL = 5.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.07
CASH_RELEASE_DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
QUALITY_SPREAD_MAX = 0.04
QUALITY_OVERROUND_MAX = 0.03
QUALITY_SIDE_SIZE_MIN = 150.0
QUALITY_BOOK_ALIGNMENT_MIN = 0.0
CANDIDATE = "h30_move_uncertainty"
STRATEGY = "h247_h30_quality_continuity"


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in sizes:
        blocks.append(runs[cursor:cursor + size])
        cursor += size
    return blocks


def quote_count_floor(frame: pd.DataFrame, selection_runs: list[str]) -> float:
    selected = frame[frame["run_name"].astype(str).isin([str(x) for x in selection_runs])]
    signal = selected[selected[f"{CANDIDATE}_side"].astype(str).ne("skip")]
    values = pd.to_numeric(signal["quote_count_first2m"], errors="coerce").dropna()
    if values.empty:
        raise ValueError("QUALITY_QUOTE_COUNT_SELECTION_MEDIAN_UNAVAILABLE")
    return float(values.median())


def _side_quality(frame: pd.DataFrame) -> pd.DataFrame:
    side = frame[f"{CANDIDATE}_side"].astype(str)
    up = side.eq("buy_up")
    down = side.eq("buy_down")
    spread = pd.Series(np.nan, index=frame.index, dtype=float)
    size = pd.Series(np.nan, index=frame.index, dtype=float)
    alignment = pd.Series(np.nan, index=frame.index, dtype=float)
    spread.loc[up] = pd.to_numeric(frame.loc[up, "spread_up_median_first2m"], errors="coerce")
    spread.loc[down] = pd.to_numeric(frame.loc[down, "spread_down_median_first2m"], errors="coerce")
    size.loc[up] = pd.to_numeric(frame.loc[up, "buy_up_size_2m"], errors="coerce")
    size.loc[down] = pd.to_numeric(frame.loc[down, "buy_down_size_2m"], errors="coerce")
    imbalance = pd.to_numeric(frame["bid_depth_imbalance_updown_2m"], errors="coerce")
    alignment.loc[up] = imbalance.loc[up]
    alignment.loc[down] = -imbalance.loc[down]
    return pd.DataFrame({"spread": spread, "size": size, "book_alignment": alignment}, index=frame.index)


def apply_quality_gate(frame: pd.DataFrame, quote_count_min: float) -> pd.DataFrame:
    out = frame.copy()
    quality = _side_quality(out)
    count = pd.to_numeric(out["quote_count_first2m"], errors="coerce")
    overround = pd.to_numeric(out["overround_median_first2m"], errors="coerce")
    signal = out[f"{CANDIDATE}_side"].astype(str).ne("skip")
    keep = (
        signal
        & count.ge(float(quote_count_min))
        & quality["spread"].le(QUALITY_SPREAD_MAX)
        & overround.le(QUALITY_OVERROUND_MAX)
        & quality["size"].ge(QUALITY_SIDE_SIZE_MIN)
        & quality["book_alignment"].ge(QUALITY_BOOK_ALIGNMENT_MIN)
    )
    out[f"{STRATEGY}_side"] = out[f"{CANDIDATE}_side"].where(keep, "skip")
    out[f"{STRATEGY}_minute"] = out[f"{CANDIDATE}_minute"]
    out[f"{STRATEGY}_fraction"] = out[f"{CANDIDATE}_fraction"].where(keep, 0.0)
    out[f"{STRATEGY}_component"] = "h30_frozen_plus_preentry_quality"
    return out


def _paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ] if not logs.empty else []


def chain_summary(logs: pd.DataFrame) -> dict[str, Any]:
    events = _paper_events(logs)
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
        "first_ten_target_growth_passed": bool(prefix["paper_replay_passed"] and endpoint >= 1000.0),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, names: list[str], slippage: float, depth_floor: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in names])],
        STRATEGY,
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
    return {"strategy": STRATEGY, "slippage": float(slippage), "depth_floor": float(depth_floor), **metrics, **chain_summary(logs)}


def _selection_pass(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"])
        and int(row["first_ten_fills"]) == REQUIRED_FILLS
        and float(row["first_ten_minimum_cash_usd"]) >= MINIMUM_ORDER_NOTIONAL
        for row in rows
    )


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    base = h54.add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    quote_min = quote_count_floor(base, selection_runs)
    frame = apply_quality_gate(base, quote_min)
    blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    selection_rows = [{"split": "selection20", **replay(frame, selection_runs, s, d)} for s in SLIPPAGES for d in DEPTH_FLOORS]
    holdout_rows = [{"split": "holdout59", **replay(frame, holdout_runs, s, d)} for s in SLIPPAGES for d in DEPTH_FLOORS]
    block_rows: list[dict[str, Any]] = []
    for idx, names in enumerate(blocks, start=1):
        for s in SLIPPAGES:
            for d in DEPTH_FLOORS:
                block_rows.append({"split": f"holdout_block_{idx}", "block_index": idx, **replay(frame, names, s, d)})
    block_gate = bool(block_rows) and all(
        bool(row["first_ten_passed"])
        and int(row["first_ten_fills"]) == REQUIRED_FILLS
        and float(row["first_ten_minimum_cash_usd"]) >= MINIMUM_ORDER_NOTIONAL
        for row in block_rows
    )
    result: dict[str, Any] = {
        "format": "old-clean-h247-h30-quality-continuity-v1",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "complete_runs": len(runs), "feature_rows": len(features), "candidate": CANDIDATE, "strategy": STRATEGY,
        "quality_gate": {
            "quote_count_first2m_min": quote_min,
            "selection_only_calibration": True,
            "spread_max": QUALITY_SPREAD_MAX, "overround_max": QUALITY_OVERROUND_MAX,
            "side_buy_size_min": QUALITY_SIDE_SIZE_MIN, "book_alignment_min": QUALITY_BOOK_ALIGNMENT_MIN,
            "fields_are_pre_entry_only": True,
        },
        "selection_rows": selection_rows, "holdout_rows": holdout_rows, "holdout_block_rows": block_rows,
        "selection_gate_passed": _selection_pass(selection_rows),
        "holdout_gate_passed": _selection_pass(holdout_rows),
        "holdout_block_gate_passed": block_gate,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL, "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES), "depth_floors": list(DEPTH_FLOORS), "same_ledger": True,
            "no_concatenation": True, "paper_only": True,
        },
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": False, "model_refit": False, "live_orders_enabled": False,
        "evidence_boundary": "old-clean pre-entry quality-gated H30 paper replay; no authenticated account receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_metrics.csv", index=False)
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"quote_count_first2m_min": quote_min, "selection_gate_passed": result["selection_gate_passed"], "holdout_gate_passed": result["holdout_gate_passed"], "holdout_block_gate_passed": block_gate}, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
