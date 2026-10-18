"""H91 stability-first selection audit for the old-clean Random Forest family.

H90 selected by total selection cash and overfit.  H91 freezes a stronger
selection rule before looking at holdout: the 20-run selection window is split
into four chronological five-run blocks, and every block must end at or above
$5 with non-negative PnL while the overall selection has at least ten fills.
The untouched 59-run holdout is then evaluated once.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

from causal_model_small_cash_replay import (
    BLOCK_SIZES,
    add_features,
    build_m1,
    make_events,
    replay,
)

LEAVES = (2, 5, 10)
THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75)
PRICE_CAPS = (0.50, 0.55, 0.60)
SPREAD_CAPS = (0.04, 0.06)
SELECTION_BLOCK_SIZE = 5


def expanding_probabilities(frame: pd.DataFrame, runs: list[str], features: list[str], leaf: int) -> pd.Series:
    ordered = frame.sort_values("first_quote_ts").reset_index(drop=True)
    probabilities = np.full(len(ordered), np.nan, dtype=float)
    for index, run_name in enumerate(runs[4:], start=4):
        train = ordered[ordered["run_name"].isin(runs[:index])]
        current = ordered[ordered["run_name"].eq(run_name)]
        train = train[train["outcome_up"].notna()]
        if len(train) < 100 or train["outcome_up"].nunique() < 2 or current.empty:
            continue
        model = Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("forest", RandomForestClassifier(
                n_estimators=160,
                min_samples_leaf=leaf,
                max_features="sqrt",
                class_weight="balanced_subsample",
                random_state=9010 + leaf,
                n_jobs=-1,
            )),
        ])
        model.fit(train[features], train["outcome_up"].astype(int))
        probabilities[current.index] = model.predict_proba(current[features])[:, 1]
    return pd.Series(probabilities, index=ordered.index)


def selection_blocks(events: pd.DataFrame, selection_runs: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for block in range(4):
        names = selection_runs[block * SELECTION_BLOCK_SIZE : (block + 1) * SELECTION_BLOCK_SIZE]
        metrics, _ = replay(events, names, 0.005)
        rows.append({"block": block + 1, "run_count": len(names), **metrics})
    return rows


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    frame, runs = build_m1(source_root)
    frame, features = add_features(frame)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    rows: list[dict[str, object]] = []
    for leaf in LEAVES:
        scored = frame.copy()
        scored["p_up"] = expanding_probabilities(scored, runs, features, leaf)
        scored["p_down"] = 1.0 - scored["p_up"]
        for threshold in THRESHOLDS:
            for price_cap in PRICE_CAPS:
                for spread_cap in SPREAD_CAPS:
                    events = make_events(scored, threshold, price_cap, spread_cap)
                    selection, _ = replay(events, selection_runs, 0.005)
                    blocks = selection_blocks(events, selection_runs)
                    holdout, _ = replay(events, holdout_runs, 0.005)
                    stable = (
                        selection["trades"] >= 10
                        and all(block["ending_cash"] >= 5.0 and block["net_pnl"] >= 0.0 for block in blocks)
                    )
                    rows.append({
                        "min_samples_leaf": leaf,
                        "threshold": threshold,
                        "price_cap": price_cap,
                        "spread_cap": spread_cap,
                        "signals": len(events),
                        "selection_trades": selection["trades"],
                        "selection_cash": selection["ending_cash"],
                        "selection_min_block_cash": min(block["ending_cash"] for block in blocks),
                        "selection_min_block_net_pnl": min(block["net_pnl"] for block in blocks),
                        "selection_stable": stable,
                        "holdout_trades": holdout["trades"],
                        "holdout_cash": holdout["ending_cash"],
                        "holdout_net_pnl": holdout["net_pnl"],
                        "selection_block_cash": ",".join(f"{block['ending_cash']:.6f}" for block in blocks),
                    })
    grid = pd.DataFrame(rows)
    eligible = grid[grid["selection_stable"]].copy()
    if eligible.empty:
        best = None
    else:
        best = eligible.sort_values(
            ["selection_min_block_cash", "selection_cash", "holdout_trades"],
            ascending=[False, False, False],
        ).iloc[0].to_dict()
    output.mkdir(parents=True, exist_ok=True)
    grid.to_csv(output / "grid.csv", index=False)
    summary = {
        "format": "old-clean-h91-random-forest-stability-first-v1",
        "source_root": str(source_root),
        "source_snapshot": source_commit,
        "complete_runs": len(runs),
        "usable_m1_markets": len(frame),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "features": features,
        "grid_cells": len(grid),
        "selection_stable_cells": len(eligible),
        "maximum_selection_trades": int(grid["selection_trades"].max()),
        "maximum_holdout_trades_all_cells": int(grid["holdout_trades"].max()),
        "maximum_holdout_trades_stable_cells": int(eligible["holdout_trades"].max()) if not eligible.empty else 0,
        "best_stable_cell": best,
        "selection_rule": "overall selection trades >= 10 and each chronological 5-run block ends >= $5 with non-negative PnL",
        "same_ledger": True,
        "required_consecutive_fills": 10,
        "initial_cash": 10.0,
        "minimum_order": 5.0,
        "fee_rate": 0.07,
        "slippage": 0.005,
        "settlement_plus_release_seconds": 900,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default="unknown")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
