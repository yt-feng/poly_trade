"""Fee-adjusted edge gate for the expanding logistic small-cash model.

The model probability is compared with the observed ask plus fixed slippage
and the Polymarket taker fee per share.  Selection happens only on the first
20 test runs; the 59-run holdout is untouched.  This remains a paper audit.
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

PMIN = (0.50, 0.55, 0.60)
EDGE_BUFFERS = (0.05, 0.06, 0.08, 0.10, 0.12, 0.15)
PRICE_CAPS = (0.50, 0.55)
SPREAD_CAPS = (0.04, 0.06)
SLIPPAGE = 0.005


def edge_filter(events: pd.DataFrame, frame: pd.DataFrame, buffer: float) -> pd.DataFrame:
    probabilities = frame.set_index("market_id")["p_up"]
    p_up = probabilities.reindex(events["market_id"]).to_numpy(dtype=float)
    side_probability = np.where(events["side"].eq("up"), p_up, 1.0 - p_up)
    fill_price = events["price"].to_numpy(dtype=float) + SLIPPAGE
    fee_per_share = 0.07 * fill_price * (1.0 - fill_price)
    edge = side_probability - (fill_price + fee_per_share)
    return events.loc[edge >= buffer].copy()


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    frame, runs = build_m1(source_root)
    frame, features = add_features(frame)
    frame["p_up"] = expanding_probabilities(frame, runs, features)
    frame["p_down"] = 1.0 - frame["p_up"]
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    rows: list[dict[str, object]] = []
    for pmin, buffer, price_cap, spread_cap in itertools.product(
        PMIN, EDGE_BUFFERS, PRICE_CAPS, SPREAD_CAPS
    ):
        events = edge_filter(
            make_events(frame, pmin, price_cap, spread_cap), frame, buffer
        )
        selection, _ = replay(events, selection_runs, SLIPPAGE)
        holdout, _ = replay(events, holdout_runs, SLIPPAGE)
        cursor = 0
        block_cash: list[float] = []
        for size in BLOCK_SIZES:
            names = test_runs[cursor : cursor + size]
            cursor += size
            block_cash.append(replay(events, names, SLIPPAGE)[0]["ending_cash"])
        rows.append(
            {
                "pmin": pmin,
                "edge_buffer": buffer,
                "price_cap": price_cap,
                "spread_cap": spread_cap,
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
        "format": "old-clean-h88-logistic-edge-gate-v1",
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "usable_m1_markets": len(frame),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "grid_cells": len(grid),
        "pmin": list(PMIN),
        "edge_buffers": list(EDGE_BUFFERS),
        "price_caps": list(PRICE_CAPS),
        "spread_caps": list(SPREAD_CAPS),
        "same_ledger": True,
        "required_fills": 10,
        "initial_cash": 10.0,
        "minimum_order": 5.0,
        "fee_rate": 0.07,
        "slippage": SLIPPAGE,
        "selection_eligible_cells": len(eligible),
        "best_eligible": eligible.head(1).to_dict(orient="records"),
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
