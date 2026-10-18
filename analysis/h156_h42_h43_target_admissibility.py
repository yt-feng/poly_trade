"""H156: target-growth admissibility for the registered causal H42/H43 models.

H155 found that the frozen H30/H38 signals produce too few opportunities after
the economic target gate.  H156 reuses the already registered expanding causal
H42 Logistic and H43 Beta-Binomial protocols, with their strongest predeclared
edge branch (0.03), and applies the same target gate before any holdout result
is read.  Probabilities for each run use only completed earlier runs.

This is an old-clean paper replay: one chronological ledger, five-share/five-
dollar floor, fee, depth, slippage, settlement and cash release are enforced;
holdout blocks are independent diagnostics and are never concatenated.  No new
data, holdout tuning, live order or authenticated Canary fill is produced.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h42_causal_calibrated_edge as h42
import h43_betabinomial_residual_calibration as h43
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
TARGET_CASH = 1000.0
REQUIRED_FILLS = 10
TARGET_MULTIPLIER = (TARGET_CASH / INITIAL_CASH) ** (1.0 / REQUIRED_FILLS)
MINIMUM_ORDER_NOTIONAL = 5.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.07
MAXIMUM_SLIPPAGE = 0.010
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
CASH_RELEASE_DELAY_SECONDS = 600.0
SETTLEMENT_SECONDS = 300.0
FIXED_EDGE_BUFFER = 0.03
RAW_EDGE_FLOOR = 0.20
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
CANDIDATES = (h42.STRATEGY, h43.STRATEGY)


def minimum_win_quote_cap(
    *,
    initial_cash: float = INITIAL_CASH,
    target_multiplier: float = TARGET_MULTIPLIER,
    minimum_cost: float = MINIMUM_ORDER_NOTIONAL,
    minimum_shares: float = MINIMUM_ORDER_SHARES,
    fee_rate: float = FEE_RATE,
    maximum_slippage: float = MAXIMUM_SLIPPAGE,
) -> float:
    """Solve the entry quote cap for a minimum-order target-growth win."""
    values = (initial_cash, target_multiplier, minimum_cost, minimum_shares, fee_rate, maximum_slippage)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("NONFINITE_TARGET_QUOTE_INPUT")
    if minimum_cost <= 0 or minimum_shares <= 0 or target_multiplier <= 1 or fee_rate < 0 or maximum_slippage < 0:
        raise ValueError("INVALID_TARGET_QUOTE_INPUT")

    def endpoint(quote: float) -> float:
        fill = quote + maximum_slippage
        fee = fee_rate * minimum_shares * (1.0 - fill)
        return initial_cash - minimum_cost - fee + minimum_shares / fill

    target = initial_cash * target_multiplier
    lo, hi = 0.05, 0.95 - maximum_slippage
    if endpoint(hi) >= target:
        return hi
    if endpoint(lo) < target:
        return lo
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if endpoint(mid) >= target:
            lo = mid
        else:
            hi = mid
    return lo


ENTRY_QUOTE_CAP = minimum_win_quote_cap()


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in sizes:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    if len({item for block in blocks for item in block}) != len(runs):
        raise ValueError("BLOCKS_MUST_NOT_OVERLAP")
    return blocks


def _entry_quote(frame: pd.DataFrame, candidate: str) -> pd.Series:
    side = frame[f"{candidate}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(frame[f"{candidate}_minute"], errors="coerce").fillna(-1).astype(int)
    quote = pd.Series(np.nan, index=frame.index, dtype=float)
    for side_name in ("buy_up", "buy_down"):
        rows = side.eq(side_name) & minute.eq(2)
        if rows.any():
            quote.loc[rows] = pd.to_numeric(frame.loc[rows, f"exec_{side_name}_2m_d4_quote_price"], errors="coerce")
    return quote


def _predicted_edge(frame: pd.DataFrame, candidate: str, probability: pd.Series) -> pd.Series:
    side = frame[f"{candidate}_side"].fillna("skip").astype(str)
    quote = _entry_quote(frame, candidate)
    fill = quote + MAXIMUM_SLIPPAGE
    fee = FEE_RATE * fill * (1.0 - fill)
    p_up = pd.to_numeric(probability, errors="coerce")
    up = p_up - fill - fee
    down = (1.0 - p_up) - fill - fee
    return pd.Series(np.where(side.eq("buy_up"), up, np.where(side.eq("buy_down"), down, np.nan)), index=frame.index)


def apply_target_gate(frame: pd.DataFrame, candidate: str, probability: pd.Series) -> tuple[pd.DataFrame, dict[str, Any]]:
    out = frame.copy()
    side = out[f"{candidate}_side"].fillna("skip").astype(str)
    quote = _entry_quote(out, candidate)
    edge = _predicted_edge(out, candidate, probability)
    keep = (
        side.isin(["buy_up", "buy_down"])
        & quote.le(ENTRY_QUOTE_CAP)
        & edge.ge(RAW_EDGE_FLOOR)
    ).fillna(False)
    out.loc[~keep, f"{candidate}_side"] = "skip"
    out.loc[~keep, f"{candidate}_fraction"] = 0.0
    return out, {
        "candidate": candidate,
        "signal_rows_before_gate": int(side.isin(["buy_up", "buy_down"]).sum()),
        "eligible_rows_after_gate": int(keep.sum()),
        "entry_quote_cap": ENTRY_QUOTE_CAP,
        "raw_edge_floor": RAW_EDGE_FLOOR,
    }


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
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
    ]


def chain_summary(logs: pd.DataFrame) -> dict[str, Any]:
    events = paper_events(logs)
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
        "first_ten_target_growth_passed": bool(prefix["paper_replay_passed"] and endpoint >= TARGET_CASH),
        "first_ten_failure_reason": prefix["failure_reason"],
        "per_fill_geometric_multiplier": (endpoint / INITIAL_CASH) ** (1.0 / REQUIRED_FILLS) if endpoint > 0 else 0.0,
    }


def replay(frame: pd.DataFrame, candidate: str, names: list[str], slippage: float, depth_floor: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
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
    return {"candidate": candidate, "slippage": float(slippage), "depth_floor": float(depth_floor), **metrics, **chain_summary(logs)}


def _all_ten_passed(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS for row in rows
    )


def _select(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        group = [row for row in rows if row["candidate"] == candidate]
        if not _all_ten_passed(group):
            continue
        eligible.append({
            "candidate": candidate,
            "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
            "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
            "target_growth_all_configs": all(bool(row["first_ten_target_growth_passed"]) for row in group),
        })
    return sorted(eligible, key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["candidate"]))[0] if eligible else None


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    h42_prob, h42_history = h42.expanding_probabilities(frame, runs)
    h43_prob = h43.expanding_probabilities(frame, runs)
    generated: dict[str, tuple[pd.DataFrame, pd.Series, dict[str, Any]]] = {}
    h42_frame = h42.add_edge_signal(frame, h42_prob, FIXED_EDGE_BUFFER)
    generated[CANDIDATES[0]] = (*apply_target_gate(h42_frame, CANDIDATES[0], h42_prob), {"history_rows_min": int(h42_history.min())})
    h43_frame = h43.add_edge_signal(frame, h43_prob, FIXED_EDGE_BUFFER)
    generated[CANDIDATES[1]] = (*apply_target_gate(h43_frame, CANDIDATES[1], h43_prob), {})

    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    selection_rows: list[dict[str, Any]] = []
    gate_stats: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        gated, stats, extra = generated[candidate]
        gate_stats.append({**stats, **extra})
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                selection_rows.append({"split": "selection20", **replay(gated, candidate, selection_runs, slippage, depth_floor)})
    choice = _select(selection_rows)
    holdout_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    if choice:
        candidate = str(choice["candidate"])
        gated = generated[candidate][0]
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                holdout_rows.append({"split": "holdout59", **replay(gated, candidate, holdout_runs, slippage, depth_floor)})
        for block_index, names in enumerate(holdout_blocks, start=1):
            for slippage in SLIPPAGES:
                for depth_floor in DEPTH_FLOORS:
                    block_rows.append({"split": f"holdout_block_{block_index}", "block_index": block_index, **replay(gated, candidate, names, slippage, depth_floor)})
    holdout_ten = _all_ten_passed(holdout_rows)
    holdout_target = bool(holdout_rows) and all(bool(row["first_ten_target_growth_passed"]) for row in holdout_rows)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS and float(row["first_ten_minimum_cash_usd"]) >= MINIMUM_ORDER_NOTIONAL
        for row in block_rows
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h156-h42-h43-target-admissibility-v1",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "complete_runs": len(runs), "feature_rows": len(frame), "candidates": list(CANDIDATES),
        "training_protocol": "H42 expanding LogisticRegression and H43 expanding Beta-Binomial use only completed earlier runs; fixed edge branch 0.03",
        "gate": {"entry_quote_cap": ENTRY_QUOTE_CAP, "raw_edge_floor": RAW_EDGE_FLOOR, "maximum_slippage": MAXIMUM_SLIPPAGE, "required_per_fill_multiplier": TARGET_MULTIPLIER, "candidate_stats": gate_stats},
        "selection_rule": "select a candidate only if every declared slippage/depth selection cell completes ten chronological fills; rank by worst first-ten cash; holdout is never used for selection",
        "selection_choice": choice, "selection_rows": selection_rows, "holdout_rows": holdout_rows, "holdout_block_rows": block_rows,
        "holdout_ten_fill_passed": holdout_ten, "holdout_target_growth_passed": holdout_target, "holdout_block_gate_passed": block_gate,
        "protocol": {"initial_cash_usd": INITIAL_CASH, "target_cash_usd": TARGET_CASH, "required_fills": REQUIRED_FILLS, "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL, "minimum_order_shares": MINIMUM_ORDER_SHARES, "fee_rate": FEE_RATE, "settlement_seconds": SETTLEMENT_SECONDS, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS, "slippages": list(SLIPPAGES), "depth_floors": list(DEPTH_FLOORS), "same_ledger": True, "no_concatenation": True, "no_holdout_tuning": True, "paper_only": True},
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0, "new_data_collected": False, "model_refit": False, "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean causal H42/H43 paper replay with target-growth gate; no authenticated order, fill, settlement or owner receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_metrics.csv", index=False)
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selection_choice": choice, "holdout_ten_fill_passed": holdout_ten, "holdout_target_growth_passed": holdout_target, "holdout_block_gate_passed": block_gate}, ensure_ascii=False, sort_keys=True))
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
