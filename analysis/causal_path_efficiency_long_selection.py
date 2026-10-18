"""Predeclared long-selection threshold audit on the unchanged old-clean data.

This is a separate protocol from the frozen 20/59 threshold grid.  It gives
the same path-efficiency thresholds forty chronological test runs for
selection and reserves the final thirty-nine runs for an independent holdout.
The selection gate includes the user's exact requirement: the first ten paper
events must be one cash-recycling chain under the same fee, delay, and minimum
order contract.  No data is collected and no order is submitted.
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
from causal_path_efficiency_threshold_grid import (
    STRATEGY,
    THRESHOLDS,
    SLIPPAGES,
    chronological_blocks,
    gated,
)
from sequential_compound_gate import evaluate_compound_chain


EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUN_COUNT = 40
HOLDOUT_RUN_COUNT = 39
SELECTION_BLOCK_SIZES = (8, 8, 8, 8, 8)
HOLDOUT_BLOCK_SIZES = (8, 8, 8, 8, 7)
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
DELAY_SECONDS = 600.0
FEE_RATE = 0.07


def _paper_events(logs: pd.DataFrame) -> list[dict[str, object]]:
    """Convert only executed paper rows into the sequential ledger contract."""
    if logs.empty:
        return []
    rows = logs.sort_values("first_quote_ts", kind="stable")
    events: list[dict[str, object]] = []
    for row in rows.itertuples(index=False):
        events.append(
            {
                "status": "paper",
                "entry_ts": float(pd.Timestamp(row.first_quote_ts).timestamp()),
                "cash_available_ts": float(pd.Timestamp(row.release_ts).timestamp()),
                "entry_cost": str(row.filled_cost),
                "fee": str(row.fee_usd),
                "payout": str(row.payout_at_settlement),
            }
        )
    return events


def compound_summary(logs: pd.DataFrame) -> dict[str, object]:
    """Return the first-ten same-ledger result without resetting cash."""
    events = _paper_events(logs)
    result = evaluate_compound_chain(
        events[:10],
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        "fills": int(result["fills"]),
        "paper_replay_passed": bool(result["paper_replay_passed"]),
        "minimum_cash": float(result["minimum_cash"]),
        "ending_cash": float(result["ending_cash"]),
        "net_pnl": float(result["net_pnl"]),
        "failure_reason": result["failure_reason"],
    }


def _run_cell(frame: pd.DataFrame, names: list[str], slippage: float) -> tuple[dict[str, object], dict[str, object]]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(names)],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
    )
    return metrics, compound_summary(logs)


def _selection_gate(rows: pd.DataFrame) -> bool:
    total = rows[rows["split"].eq("selection40")]
    blocks = rows[rows["split"].str.startswith("selection_block")]
    return bool(
        not total.empty
        and int(total.iloc[0]["trades"]) >= 10
        and (total["compound_paper_replay_passed"] == True).all()
        and len(blocks) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES)
        and (blocks["ending_cash"] >= MINIMUM_ORDER).all()
        and (blocks["net_return"] >= 0.0).all()
    )


def _holdout_gate(rows: pd.DataFrame) -> bool:
    total = rows[rows["split"].eq("holdout39")]
    blocks = rows[rows["split"].str.startswith("holdout_block")]
    return bool(
        not total.empty
        and int(total.iloc[0]["trades"]) >= 10
        and (total["compound_paper_replay_passed"] == True).all()
        and len(blocks) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES)
        and (blocks["ending_cash"] >= MINIMUM_ORDER).all()
        and (blocks["net_return"] >= 0.0).all()
    )


def select_threshold(metrics: pd.DataFrame) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        rows = metrics[metrics["threshold"].eq(threshold)]
        selection = rows[rows["split"].str.startswith("selection")]
        total = selection[
            selection["split"].eq("selection40") & selection["slippage"].eq(SLIPPAGES[0])
        ]
        blocks = selection[
            selection["split"].str.startswith("selection_block")
            & selection["slippage"].eq(SLIPPAGES[1])
        ]
        candidates.append(
            {
                "threshold": float(threshold),
                "selection_gate": _selection_gate(selection),
                "selection_trades": int(total.iloc[0]["trades"]) if not total.empty else 0,
                "selection_ending_cash_005": float(total.iloc[0]["ending_cash"]) if not total.empty else 0.0,
                "selection_min_block_cash_010": float(blocks["ending_cash"].min()) if not blocks.empty else 0.0,
                "selection_prefix_ending_cash_005": float(total.iloc[0]["compound_ending_cash"])
                if not total.empty else 0.0,
            }
        )
    admissible = [row for row in candidates if row["selection_gate"]]
    if not admissible:
        return {"selected_threshold": None, "selection_candidates": candidates, "selection_gate_found": False}
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
    observed = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUN_COUNT]
    holdout_runs = test_runs[SELECTION_RUN_COUNT:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    rows: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        frame = gated(features, threshold)
        splits: list[tuple[str, list[str]]] = [("selection40", selection_runs)]
        splits += [(f"selection_block{index}", names) for index, names in enumerate(selection_blocks, 1)]
        splits += [("holdout39", holdout_runs)]
        splits += [(f"holdout_block{index}", names) for index, names in enumerate(holdout_blocks, 1)]
        for split, names in splits:
            for slippage in SLIPPAGES:
                metrics, compound = _run_cell(frame, names, slippage)
                rows.append(
                    {
                        "threshold": float(threshold),
                        "split": split,
                        "slippage": float(slippage),
                        **metrics,
                        "compound_fills": compound["fills"],
                        "compound_paper_replay_passed": compound["paper_replay_passed"],
                        "compound_minimum_cash": compound["minimum_cash"],
                        "compound_ending_cash": compound["ending_cash"],
                        "compound_net_pnl": compound["net_pnl"],
                        "compound_failure_reason": compound["failure_reason"],
                    }
                )

    result = pd.DataFrame(rows)
    selection = select_threshold(result)
    selected_threshold = selection["selected_threshold"]
    selected_holdout: dict[str, object] | None = None
    holdout_gate = False
    if selected_threshold is not None:
        selected = result[result["threshold"].eq(selected_threshold)]
        holdout = selected[selected["split"].str.startswith("holdout")]
        holdout_gate = _holdout_gate(holdout)
        total = holdout[holdout["split"].eq("holdout39") & holdout["slippage"].eq(SLIPPAGES[0])]
        blocks = holdout[holdout["split"].str.startswith("holdout_block") & holdout["slippage"].eq(SLIPPAGES[1])]
        selected_holdout = {
            "holdout_gate": bool(holdout_gate),
            "holdout_trades_005": int(total.iloc[0]["trades"]) if not total.empty else 0,
            "holdout_ending_cash_005": float(total.iloc[0]["ending_cash"]) if not total.empty else 0.0,
            "holdout_prefix_paper_replay_005": bool(total.iloc[0]["compound_paper_replay_passed"])
            if not total.empty else False,
            "holdout_prefix_fills_005": int(total.iloc[0]["compound_fills"]) if not total.empty else 0,
            "minimum_holdout_block_cash_010": float(blocks["ending_cash"].min()) if not blocks.empty else 0.0,
            "minimum_holdout_block_net_return_010": float(blocks["net_return"].min()) if not blocks.empty else 0.0,
        }

    meta: dict[str, object] = {
        "format": "old-data-causal-path-efficiency-long-selection-v1",
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
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "required_compound_fills": 10,
        },
        "selection_rule": "selection40 has >=10 trades, first ten same-ledger paper replay passes at every slippage, and every selection block ends >=$5 with nonnegative return at every slippage; rank by minimum 1% block cash, then 0.5% cash, then lower threshold",
        "selection": selection,
        "selected_threshold": selected_threshold,
        "selected_holdout": selected_holdout,
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "Historical old-clean paper replay only; compound prefix is an upper bound and contains no authenticated order, fill, settlement, or reusable-cash receipt.",
        "decision": "holdout_gate_required_before_any_promotion",
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
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
