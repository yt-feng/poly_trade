"""Direction-alignment challenger for the causal logistic small-cash model.

The alignment threshold is frozen before the untouched holdout is read.  A
gate that removes the bad block only counts as useful if it still supplies a
ten-fill holdout chain; otherwise it is an abstention diagnostic, not a
strategy.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from causal_model_small_cash_replay import (
    BLOCK_SIZES,
    add_features,
    build_m1,
    expanding_probabilities,
    make_events,
    replay,
)

THRESHOLDS = (0.60, 0.62, 0.65, 0.70)
PRICE_CAPS = (0.48, 0.49, 0.50)
SPREAD_CAPS = (0.04, 0.06)
ALIGNMENT_GATES = ("none", "same_sign", "strong_5", "strong_10")


def apply_alignment(events: pd.DataFrame, frame: pd.DataFrame, gate: str) -> pd.DataFrame:
    if gate == "none":
        return events
    move = pd.to_numeric(frame.set_index("market_id")["btc_move_1m"], errors="coerce")
    signed = events["market_id"].map(move)
    signed = np.where(events["side"].eq("up"), signed, -signed)
    minimum = {"same_sign": 0.0, "strong_5": 5.0, "strong_10": 10.0}[gate]
    return events.loc[np.asarray(signed >= minimum)].copy()


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    frame, runs = build_m1(source_root)
    frame, features = add_features(frame)
    frame["p_up"] = expanding_probabilities(frame, runs, features)
    frame["p_down"] = 1.0 - frame["p_up"]
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    rows: list[dict[str, object]] = []
    for threshold, price_cap, spread_cap, gate in itertools.product(
        THRESHOLDS, PRICE_CAPS, SPREAD_CAPS, ALIGNMENT_GATES
    ):
        events = apply_alignment(
            make_events(frame, threshold, price_cap, spread_cap), frame, gate
        )
        selection, _ = replay(events, selection_runs, 0.005)
        holdout, _ = replay(events, holdout_runs, 0.005)
        cursor = 0
        block_cash: list[float] = []
        for size in BLOCK_SIZES:
            names = test_runs[cursor : cursor + size]
            cursor += size
            block_cash.append(replay(events, names, 0.005)[0]["ending_cash"])
        rows.append(
            {
                "threshold": threshold,
                "price_cap": price_cap,
                "spread_cap": spread_cap,
                "alignment_gate": gate,
                "signals": len(events),
                "selection_trades": selection["trades"],
                "selection_cash": selection["ending_cash"],
                "holdout_trades": holdout["trades"],
                "holdout_cash": holdout["ending_cash"],
                "min_block_cash": min(block_cash),
                "block_cash": ",".join(f"{value:.6f}" for value in block_cash),
            }
        )
    grid = pd.DataFrame(rows)
    eligible = grid[grid["selection_trades"].ge(10)].sort_values(
        ["min_block_cash", "selection_cash"], ascending=[False, False]
    )
    output.mkdir(parents=True, exist_ok=True)
    grid.to_csv(output / "grid.csv", index=False)
    summary = {
        "format": "old-clean-h87-logistic-alignment-gate-v1",
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "usable_m1_markets": len(frame),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "grid_cells": len(grid),
        "thresholds": list(THRESHOLDS),
        "price_caps": list(PRICE_CAPS),
        "spread_caps": list(SPREAD_CAPS),
        "alignment_gates": list(ALIGNMENT_GATES),
        "same_ledger": True,
        "required_fills": 10,
        "initial_cash": 10.0,
        "minimum_order": 5.0,
        "fee_rate": 0.07,
        "slippage": 0.005,
        "selection_eligible_cells": len(eligible),
        "best_selection_capacity_cell": eligible.head(1).to_dict(orient="records"),
        "best_holdout_capacity_cell": grid[grid["selection_trades"].ge(10)]
        .sort_values(["holdout_trades", "min_block_cash"], ascending=[False, False])
        .head(1)
        .to_dict(orient="records"),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()
