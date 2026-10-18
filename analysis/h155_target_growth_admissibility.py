"""H155: target-growth admissibility gate on unchanged old-clean data.

H154 showed that a sizing replay can survive ten chronological paper fills but
does not approach the user's $10 -> $1,000 objective.  H155 therefore freezes
an economic gate before looking at holdout rows: with the five-share/five-dollar
floor and the documented taker fee, an entry must be cheap enough that a win on
the minimum order can meet the required per-fill geometric multiplier, and its
causal raw edge must clear a predeclared threshold.  The frozen H30/H38 signal
definitions remain unchanged; the gate only abstains from their entries.

The experiment uses the existing 83-run old-clean snapshot, one chronological
cash ledger per split, explicit settlement/cash release, and independent
holdout blocks.  It never fits a model, collects data, submits an order, or
concatenates blocks into a Canary chain.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h55_tick_depth_prefix_audit as h55
import h57_causal_loss_window_diagnostic as h57
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
CASH_RELEASE_DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
RAW_EDGE_FLOOR = 0.20
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def minimum_win_price_cap(
    *,
    initial_cash: float = INITIAL_CASH,
    target_multiplier: float = TARGET_MULTIPLIER,
    minimum_cost: float = MINIMUM_ORDER_NOTIONAL,
    minimum_shares: float = MINIMUM_ORDER_SHARES,
    fee_rate: float = FEE_RATE,
) -> float:
    """Return the largest p where a five-share-floor win can hit the target.

    At the $10 starting balance the notional floor binds.  For a binary share
    bought at p, the minimum-order win endpoint is
    ``cash - (cost + fee) + shares`` with ``cost=minimum_cost`` and
    ``shares=minimum_shares/p``.  The gate solves that entry-time inequality;
    it is a target contract, not a label-derived threshold.
    """
    if not all(math.isfinite(float(x)) for x in (initial_cash, target_multiplier, minimum_cost, minimum_shares, fee_rate)):
        raise ValueError("NONFINITE_TARGET_PRICE_INPUT")
    if minimum_cost <= 0 or minimum_shares <= 0 or target_multiplier <= 1 or fee_rate < 0:
        raise ValueError("INVALID_TARGET_PRICE_INPUT")

    def endpoint(price: float) -> float:
        fee = fee_rate * minimum_shares * (1.0 - price)
        return initial_cash - minimum_cost - fee + minimum_shares / price

    # endpoint decreases over the relevant price interval [0.05, 0.95].
    lo, hi = 0.05, 0.95
    target = initial_cash * target_multiplier
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


ENTRY_PRICE_CAP = minimum_win_price_cap()
CANDIDATES = h55.CANDIDATES


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


def apply_target_gate(frame: pd.DataFrame, candidate: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Abstain from entries that cannot meet the frozen target contract."""
    out = frame.copy()
    entries = h57.entry_table(out, candidate)
    keep = (
        entries["signal"]
        & entries["quote_price"].le(ENTRY_PRICE_CAP)
        & entries["raw_edge"].ge(RAW_EDGE_FLOOR)
    ).fillna(False)
    out.loc[~keep, f"{candidate}_side"] = "skip"
    out.loc[~keep, f"{candidate}_fraction"] = 0.0
    return out, {
        "candidate": candidate,
        "entry_price_cap": ENTRY_PRICE_CAP,
        "raw_edge_floor": RAW_EDGE_FLOOR,
        "eligible_rows": int(keep.sum()),
        "signal_rows_before_gate": int(entries["signal"].sum()),
    }


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    rows = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in rows.itertuples(index=False)
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
    return {
        "candidate": candidate,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        **metrics,
        **chain_summary(logs),
    }


def _all_ten_passed(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and len(rows) == len(SLIPPAGES) * len(DEPTH_FLOORS) and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS for row in rows
    )


def select(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        group = [row for row in rows if row["candidate"] == candidate]
        if not _all_ten_passed(group):
            continue
        eligible.append(
            {
                "candidate": candidate,
                "worst_first_ten_cash_usd": min(float(row["first_ten_ending_cash_usd"]) for row in group),
                "sum_first_ten_cash_usd": sum(float(row["first_ten_ending_cash_usd"]) for row in group),
                "target_growth_all_configs": all(bool(row["first_ten_target_growth_passed"]) for row in group),
            }
        )
    return sorted(
        eligible,
        key=lambda row: (-row["worst_first_ten_cash_usd"], -row["sum_first_ten_cash_usd"], row["candidate"]),
    )[0] if eligible else None


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    gated_frames: dict[str, pd.DataFrame] = {}
    gate_stats: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        gated_frames[candidate], stats = apply_target_gate(frame, candidate)
        gate_stats.append(stats)

    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    selection_rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                selection_rows.append({"split": "selection20", **replay(gated_frames[candidate], candidate, selection_runs, slippage, depth_floor)})
    choice = select(selection_rows)
    holdout_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    if choice:
        candidate = str(choice["candidate"])
        for slippage in SLIPPAGES:
            for depth_floor in DEPTH_FLOORS:
                holdout_rows.append({"split": "holdout59", **replay(gated_frames[candidate], candidate, holdout_runs, slippage, depth_floor)})
        for block_index, names in enumerate(holdout_blocks, start=1):
            for slippage, depth_floor in ((SLIPPAGES[0], DEPTH_FLOORS[0]), (SLIPPAGES[1], DEPTH_FLOORS[1])):
                block_rows.append({"split": f"holdout_block_{block_index}", "block_index": block_index, **replay(gated_frames[candidate], candidate, names, slippage, depth_floor)})
    chosen_holdout_passed = _all_ten_passed(holdout_rows)
    chosen_holdout_target = bool(holdout_rows) and all(bool(row["first_ten_target_growth_passed"]) for row in holdout_rows)
    block_gate = bool(block_rows) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * 2 and all(
        bool(row["first_ten_passed"]) and int(row["first_ten_fills"]) == REQUIRED_FILLS and float(row["first_ten_minimum_cash_usd"]) >= MINIMUM_ORDER_NOTIONAL
        for row in block_rows
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h155-target-growth-admissibility-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(CANDIDATES),
        "gate": {
            "entry_price_cap": ENTRY_PRICE_CAP,
            "raw_edge_floor": RAW_EDGE_FLOOR,
            "required_per_fill_multiplier": TARGET_MULTIPLIER,
            "derivation": "minimum five-share/five-dollar order win after 7% taker fee must reach the target multiplier from the $10 starting cash",
            "candidate_gate_stats": gate_stats,
        },
        "selection_rule": "select the candidate whose every declared selection cost/depth cell completes ten chronological fills; rank by worst first-ten cash; target growth is reported separately and never selected from holdout",
        "selection_choice": choice,
        "selection_rows": selection_rows,
        "holdout_rows": holdout_rows,
        "holdout_block_rows": block_rows,
        "holdout_ten_fill_passed": chosen_holdout_passed,
        "holdout_target_growth_passed": chosen_holdout_target,
        "holdout_block_gate_passed": block_gate,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "target_cash_usd": TARGET_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL,
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
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean quote/depth paper replay with a predeclared target-growth gate; no authenticated order, fill, settlement or owner receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_metrics.csv", index=False)
    pd.DataFrame(holdout_rows).to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(block_rows).to_csv(output / "holdout_block_metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "selection_choice": choice,
        "holdout_ten_fill_passed": chosen_holdout_passed,
        "holdout_target_growth_passed": chosen_holdout_target,
        "holdout_block_gate_passed": block_gate,
    }, ensure_ascii=False, sort_keys=True))
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
