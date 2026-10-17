"""Chronological old-data threshold grid for the frozen path-efficiency signal.

The grid is predeclared before reading the independent holdout result.  A
threshold may be selected using only the first twenty test runs and their four
small selection blocks; the selected threshold is then evaluated on the
following fifty-nine runs and five disjoint chronological blocks.  Every cell
uses the same $10 cash ledger, $5 minimum order, taker fee, settlement delay,
and slippage contract.  This is a paper audit only and never creates orders.
"""
from __future__ import annotations

import argparse
import json
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


STRATEGY = "v1_adaptive_mix"
THRESHOLDS = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65)
SLIPPAGES = (0.005, 0.010, 0.015)
DELAY_SECONDS = 600.0
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in sizes:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def gated(features: pd.DataFrame, threshold: float) -> pd.DataFrame:
    out = features.copy()
    keep = pd.to_numeric(
        out["path_efficiency_first2m"], errors="coerce"
    ).ge(float(threshold))
    out.loc[~keep, f"{STRATEGY}_side"] = "skip"
    out.loc[~keep, f"{STRATEGY}_minute"] = -1
    out.loc[~keep, f"{STRATEGY}_fraction"] = 0.0
    return out


def _simulate(frame: pd.DataFrame, slippage: float) -> dict[str, object]:
    _logs, metrics = simulate_cash_locked(
        frame,
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        10.0,
        5.0,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=False,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
    )
    return metrics


def _selection_gate(rows: pd.DataFrame) -> bool:
    blocks = rows[rows["split"].str.startswith("selection_block")]
    total = rows[rows["split"].eq("selection20")]
    return bool(
        not total.empty
        and int(total.iloc[0]["trades"]) >= 10
        and len(blocks) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES)
        and (blocks["ending_cash"] >= 5.0).all()
        and (blocks["net_return"] >= 0.0).all()
    )


def _holdout_gate(rows: pd.DataFrame) -> bool:
    blocks = rows[rows["split"].str.startswith("holdout_block")]
    total = rows[rows["split"].eq("holdout59")]
    return bool(
        not total.empty
        and int(total.iloc[0]["trades"]) >= 10
        and len(blocks) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES)
        and (blocks["ending_cash"] >= 5.0).all()
        and (blocks["net_return"] >= 0.0).all()
    )


def select_threshold(metrics: pd.DataFrame) -> dict[str, object]:
    """Select by selection-only evidence, then expose holdout separately."""
    candidates: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        rows = metrics[metrics["threshold"].eq(threshold)]
        selection = rows[rows["split"].str.startswith("selection")]
        selection_005 = selection[selection["slippage"].eq(SLIPPAGES[0])]
        blocks_010 = selection[
            selection["split"].str.startswith("selection_block")
            & selection["slippage"].eq(SLIPPAGES[1])
        ]
        candidates.append(
            {
                "threshold": float(threshold),
                "selection_gate": _selection_gate(selection),
                "selection_trades": int(
                    selection_005[selection_005["split"].eq("selection20")]
                    .iloc[0]["trades"]
                )
                if not selection_005[selection_005["split"].eq("selection20")].empty
                else 0,
                "selection_ending_cash_005": float(
                    selection_005[selection_005["split"].eq("selection20")]
                    .iloc[0]["ending_cash"]
                )
                if not selection_005[selection_005["split"].eq("selection20")].empty
                else 0.0,
                "selection_min_block_cash_010": float(
                    blocks_010["ending_cash"].min()
                )
                if not blocks_010.empty
                else 0.0,
            }
        )
    admissible = [row for row in candidates if row["selection_gate"]]
    if not admissible:
        return {
            "selected_threshold": None,
            "selection_candidates": candidates,
            "selection_gate_found": False,
        }
    selected = sorted(
        admissible,
        key=lambda row: (
            -float(row["selection_min_block_cash_010"]),
            -float(row["selection_ending_cash_005"]),
            float(row["threshold"]),
        ),
    )[0]
    return {
        "selected_threshold": float(selected["threshold"]),
        "selection_candidates": candidates,
        "selection_gate_found": True,
        "selected_selection_summary": selected,
    }


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")

    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    rows: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        frame = gated(features, threshold)
        splits = [("selection20", selection_runs)]
        splits += [
            (f"selection_block{index}", names)
            for index, names in enumerate(selection_blocks, 1)
        ]
        splits += [("holdout59", holdout_runs)]
        splits += [
            (f"holdout_block{index}", names)
            for index, names in enumerate(holdout_blocks, 1)
        ]
        for split, names in splits:
            subset = frame[frame["run_name"].isin(names)]
            for slippage in SLIPPAGES:
                metrics = _simulate(subset, slippage)
                rows.append(
                    {
                        "threshold": float(threshold),
                        "split": split,
                        "slippage": float(slippage),
                        **metrics,
                    }
                )

    result = pd.DataFrame(rows)
    selection = select_threshold(result)
    selected_threshold = selection["selected_threshold"]
    selected_holdout_gate = False
    selected_holdout_summary: dict[str, object] | None = None
    if selected_threshold is not None:
        selected_rows = result[result["threshold"].eq(selected_threshold)]
        selected_holdout_gate = _holdout_gate(
            selected_rows[selected_rows["split"].str.startswith("holdout")]
        )
        holdout_005 = selected_rows[
            selected_rows["split"].eq("holdout59")
            & selected_rows["slippage"].eq(SLIPPAGES[0])
        ]
        blocks = selected_rows[
            selected_rows["split"].str.startswith("holdout_block")
            & selected_rows["slippage"].eq(SLIPPAGES[1])
        ]
        selected_holdout_summary = {
            "holdout_gate": bool(selected_holdout_gate),
            "holdout_trades_005": int(holdout_005.iloc[0]["trades"])
            if not holdout_005.empty
            else 0,
            "holdout_ending_cash_005": float(holdout_005.iloc[0]["ending_cash"])
            if not holdout_005.empty
            else 0.0,
            "minimum_holdout_block_cash_010": float(blocks["ending_cash"].min())
            if not blocks.empty
            else 0.0,
            "minimum_holdout_block_net_return_010": float(blocks["net_return"].min())
            if not blocks.empty
            else 0.0,
        }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    meta: dict[str, object] = {
        "format": "old-data-causal-path-efficiency-threshold-grid-v1",
        "source_repo_commit": source_commit,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "strategy": STRATEGY,
        "predeclared_thresholds": list(THRESHOLDS),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "selection_block_sizes": list(SELECTION_BLOCK_SIZES),
        "holdout_block_sizes": list(HOLDOUT_BLOCK_SIZES),
        "protocol": {
            "initial_cash": 10.0,
            "minimum_order_notional": 5.0,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "cooldown_events": 0,
        },
        "selection_rule": "selection20 trades >= 10 and every selection block ends >= $5 with nonnegative return at all slippages; rank by minimum 1% selection-block cash, then 0.5% selection cash, then lower threshold",
        "selection": selection,
        "selected_threshold": selected_threshold,
        "selected_holdout": selected_holdout_summary,
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "Historical old-clean paper replay only; no authenticated order, fill, settlement, reusable-cash receipt or Canary fill.",
        "decision": "holdout_gate_required_before_any_promotion",
    }
    (output / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
