"""Protocol-boundary grid for the unchanged old-clean compound experiment.

This audit does not alter the frozen model or claim that a zero-fee row is a
maker fill.  It asks a narrower causal question: for the already registered
path-efficiency signal, what starting cash and fee contract are required for
the same-ledger ten-fill paper prefix to survive on independent chronological
blocks?  The source is the existing cleaned quote/label snapshot only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_path_efficiency_threshold_grid import (
    STRATEGY,
    THRESHOLDS,
    SLIPPAGES,
    chronological_blocks,
    gated,
)
from sequential_compound_gate import evaluate_compound_chain


EXPECTED_COMPLETE_RUNS = 83
INITIAL_CASH_GRID = (10.0, 10.25, 10.5, 11.0, 12.0, 15.0, 20.0)
FEE_PROTOCOLS = (("taker_0p07", 0.07), ("zero_fee_proxy", 0.0))
SELECTION_COUNT = 20
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
MINIMUM_ORDER = 5.0
DELAY_SECONDS = 600.0


def _paper_events(logs: pd.DataFrame) -> list[dict[str, object]]:
    if logs.empty:
        return []
    rows = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": float(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": float(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in rows.itertuples(index=False)
    ]


def compound_prefix(logs: pd.DataFrame, initial_cash: float) -> dict[str, object]:
    result = evaluate_compound_chain(
        _paper_events(logs)[:10],
        initial_cash=str(initial_cash),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        "fills": int(result["fills"]),
        "paper_replay_passed": bool(result["paper_replay_passed"]),
        "minimum_cash": float(result["minimum_cash"]),
        "ending_cash": float(result["ending_cash"]),
        "failure_reason": result["failure_reason"],
    }


def simulate_cell(frame: pd.DataFrame, names: list[str], initial_cash: float,
                  fee_rate: float, slippage: float) -> tuple[dict[str, object], dict[str, object]]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(names)],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        initial_cash,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=fee_rate,
    )
    return metrics, compound_prefix(logs, initial_cash)


def selection_gate(rows: pd.DataFrame) -> bool:
    total = rows[rows["split"].eq("selection20")]
    blocks = rows[rows["split"].str.startswith("selection_block")]
    return bool(
        not total.empty
        and int(total.iloc[0]["trades"]) >= 10
        and bool(total.iloc[0]["compound_paper_replay_passed"])
        and len(blocks) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES)
        and (blocks["ending_cash"] >= MINIMUM_ORDER).all()
        and (blocks["net_return"] >= 0.0).all()
    )


def holdout_gate(rows: pd.DataFrame) -> bool:
    total = rows[rows["split"].eq("holdout59")]
    blocks = rows[rows["split"].str.startswith("holdout_block")]
    return bool(
        not total.empty
        and int(total.iloc[0]["trades"]) >= 10
        and bool(total.iloc[0]["compound_paper_replay_passed"])
        and len(blocks) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES)
        and (blocks["ending_cash"] >= MINIMUM_ORDER).all()
        and (blocks["net_return"] >= 0.0).all()
    )


def choose_selection(rows: pd.DataFrame) -> dict[str, object]:
    """Select a threshold using selection blocks only for one protocol cell."""
    candidates: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        part = rows[rows["threshold"].eq(threshold)]
        selection = part[part["split"].str.startswith("selection")]
        total = selection[
            selection["split"].eq("selection20") & selection["slippage"].eq(SLIPPAGES[0])
        ]
        blocks = selection[
            selection["split"].str.startswith("selection_block")
            & selection["slippage"].eq(SLIPPAGES[1])
        ]
        candidates.append(
            {
                "threshold": float(threshold),
                "selection_gate": selection_gate(selection),
                "selection_trades": int(total.iloc[0]["trades"]) if not total.empty else 0,
                "selection_min_block_cash": float(blocks["ending_cash"].min()) if not blocks.empty else 0.0,
                "selection_ending_cash": float(total.iloc[0]["ending_cash"]) if not total.empty else 0.0,
            }
        )
    admissible = [row for row in candidates if row["selection_gate"]]
    if not admissible:
        return {"selected_threshold": None, "selection_gate_found": False, "selection_candidates": candidates}
    selected = sorted(
        admissible,
        key=lambda row: (-float(row["selection_min_block_cash"]),
                         -float(row["selection_ending_cash"]), float(row["threshold"])),
    )[0]
    return {
        "selected_threshold": float(selected["threshold"]),
        "selection_gate_found": True,
        "selection_candidates": candidates,
        "selected_selection_summary": selected,
    }


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_COUNT]
    holdout_runs = test_runs[SELECTION_COUNT:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    rows: list[dict[str, object]] = []
    for fee_name, fee_rate in FEE_PROTOCOLS:
        for initial_cash in INITIAL_CASH_GRID:
            for threshold in THRESHOLDS:
                frame = gated(features, threshold)
                splits: list[tuple[str, list[str]]] = [("selection20", selection_runs)]
                splits += [(f"selection_block{i}", names) for i, names in enumerate(selection_blocks, 1)]
                splits += [("holdout59", holdout_runs)]
                splits += [(f"holdout_block{i}", names) for i, names in enumerate(holdout_blocks, 1)]
                for split, names in splits:
                    for slippage in SLIPPAGES:
                        metrics, compound = simulate_cell(frame, names, initial_cash, fee_rate, slippage)
                        rows.append({
                            "fee_protocol": fee_name,
                            "fee_rate": fee_rate,
                            "initial_cash": initial_cash,
                            "threshold": float(threshold),
                            "split": split,
                            "slippage": float(slippage),
                            **metrics,
                            "compound_fills": compound["fills"],
                            "compound_paper_replay_passed": compound["paper_replay_passed"],
                            "compound_minimum_cash": compound["minimum_cash"],
                            "compound_ending_cash": compound["ending_cash"],
                            "compound_failure_reason": compound["failure_reason"],
                        })

    result = pd.DataFrame(rows)
    selected_rows: list[dict[str, object]] = []
    for fee_name, fee_rate in FEE_PROTOCOLS:
        for initial_cash in INITIAL_CASH_GRID:
            cell = result[(result["fee_protocol"].eq(fee_name)) & (result["initial_cash"].eq(initial_cash))]
            choice = choose_selection(cell)
            threshold = choice["selected_threshold"]
            if threshold is None:
                selected_rows.append({"fee_protocol": fee_name, "fee_rate": fee_rate, "initial_cash": initial_cash,
                                      "selected_threshold": None, "selection_gate": False, "holdout_gate": False,
                                      "selection_min_block_cash": 0.0, "holdout_min_block_cash": 0.0})
                continue
            chosen = cell[cell["threshold"].eq(threshold)]
            sel = chosen[chosen["split"].str.startswith("selection")]
            hold = chosen[chosen["split"].str.startswith("holdout")]
            holdout_blocks = hold[hold["split"].str.startswith("holdout_block") & hold["slippage"].eq(SLIPPAGES[1])]
            selection_blocks = sel[sel["split"].str.startswith("selection_block") & sel["slippage"].eq(SLIPPAGES[1])]
            selected_rows.append({
                "fee_protocol": fee_name, "fee_rate": fee_rate, "initial_cash": initial_cash,
                "selected_threshold": threshold, "selection_gate": bool(choice["selection_gate_found"]),
                "holdout_gate": holdout_gate(hold),
                "selection_min_block_cash": float(selection_blocks["ending_cash"].min()) if not selection_blocks.empty else 0.0,
                "holdout_min_block_cash": float(holdout_blocks["ending_cash"].min()) if not holdout_blocks.empty else 0.0,
                "holdout_trades": int(hold[hold["split"].eq("holdout59") & hold["slippage"].eq(SLIPPAGES[0])].iloc[0]["trades"]) if not hold[hold["split"].eq("holdout59") & hold["slippage"].eq(SLIPPAGES[0])].empty else 0,
                "holdout_ending_cash": float(hold[hold["split"].eq("holdout59") & hold["slippage"].eq(SLIPPAGES[0])].iloc[0]["ending_cash"]) if not hold[hold["split"].eq("holdout59") & hold["slippage"].eq(SLIPPAGES[0])].empty else 0.0,
            })

    selected = pd.DataFrame(selected_rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "capital_floor_metrics.csv", index=False)
    selected.to_csv(output / "selected_protocol_cells.csv", index=False)
    passing = selected[selected["holdout_gate"].eq(True)]
    meta = {
        "format": "old-clean-capital-floor-protocol-grid-sealed-v1",
        "source_repo_commit": source_commit,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "predeclared_thresholds": list(THRESHOLDS),
        "predeclared_initial_cash_grid": list(INITIAL_CASH_GRID),
        "predeclared_fee_protocols": [{"name": n, "fee_rate": r} for n, r in FEE_PROTOCOLS],
        "protocol": {"minimum_order_notional": MINIMUM_ORDER, "cash_release_delay_seconds": DELAY_SECONDS,
                      "slippages": list(SLIPPAGES), "required_same_ledger_fills": 10},
        "selected_protocol_cells": selected_rows,
        "minimum_holdout_passing_initial_cash": float(passing["initial_cash"].min()) if not passing.empty else None,
        "decision": "report_protocol_boundary_only; do_not_promote_quote_paper_to_canary",
        "paper_only": True, "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
        "evidence_boundary": "Quote/label paper replay; zero-fee rows are protocol sensitivity proxies and do not prove maker fills, queue position or settlement.",
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
