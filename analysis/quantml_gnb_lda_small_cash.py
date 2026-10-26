"""Causal GaussianNB/LDA challengers on the immutable old-clean BTC5m data.

The model is fit only on complete runs strictly before each scored run.  The
execution grid is frozen before the run, and the selected cell is evaluated on
the chronological holdout using the existing $10/$5/7%-fee/900-second cash
release paper contract.  This is a paper diagnostic only: no order or canary
is created.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import GaussianNB
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import brier_score_loss, log_loss

from order_floor_contract import (
    DEFAULT_MINIMUM_ORDER_SHARES,
    DEFAULT_MINIMUM_ORDER_USD,
    required_order_cost,
)
from causal_model_small_cash_replay import (
    add_features,
    build_m1,
    make_events,
    replay,
)


MODEL_NAMES = ("logistic_control", "gaussian_nb", "lda")
THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
PRICE_CAPS = (0.50, 0.55, 0.60)
SPREAD_CAPS = (0.04, 0.06)
SLIPPAGES = (0.005, 0.010)
SELECTION_BLOCK_SIZE = 5
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def make_model(name: str) -> Pipeline:
    if name == "gaussian_nb":
        estimator: Any = GaussianNB(var_smoothing=1e-9)
        steps = [("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler()), ("model", estimator)]
    elif name == "lda":
        estimator = LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto")
        steps = [("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler()), ("model", estimator)]
    elif name == "logistic_control":
        estimator = LogisticRegression(C=0.5, max_iter=600)
        steps = [("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler()), ("model", estimator)]
    else:
        raise ValueError(f"unknown_model:{name}")
    return Pipeline(steps)


def expanding_probabilities(frame: pd.DataFrame, runs: list[str], features: list[str], model_name: str) -> tuple[pd.Series, int, int]:
    ordered = frame.sort_values("first_quote_ts").reset_index(drop=True)
    probabilities = np.full(len(ordered), np.nan, dtype=float)
    fit_calls = 0
    predict_calls = 0
    for index, run_name in enumerate(runs[4:], start=4):
        train = ordered[ordered["run_name"].isin(runs[:index])]
        current = ordered[ordered["run_name"].eq(run_name)]
        train = train[train["outcome_up"].notna()]
        if len(train) < 100 or train["outcome_up"].nunique() < 2 or current.empty:
            continue
        model = make_model(model_name)
        model.fit(train[features], train["outcome_up"].astype(int))
        fit_calls += 1
        probabilities[current.index] = model.predict_proba(current[features])[:, 1]
        predict_calls += 1
    return pd.Series(probabilities, index=ordered.index), fit_calls, predict_calls


def block_names(names: list[str], sizes: tuple[int, ...]) -> list[list[str]]:
    if len(names) != sum(sizes):
        raise ValueError("block_scope_mismatch")
    out: list[list[str]] = []
    cursor = 0
    for size in sizes:
        out.append(names[cursor : cursor + size])
        cursor += size
    return out


def replay_metrics(events: pd.DataFrame, names: list[str], slippage: float) -> dict[str, Any]:
    metrics, logs = replay(
        events,
        names,
        slippage,
        minimum_order_usd=DEFAULT_MINIMUM_ORDER_USD,
        minimum_order_shares=DEFAULT_MINIMUM_ORDER_SHARES,
    )
    metrics = dict(metrics)
    metrics["min_cash_after_entry"] = float(logs["cash_after_entry"].min()) if not logs.empty else 10.0
    metrics["resolved_trades"] = int(metrics.get("trades", 0))
    return metrics


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    frame, runs = build_m1(source_root)
    frame, features = add_features(frame)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    holdout_blocks = block_names(holdout_runs, HOLDOUT_BLOCK_SIZES)

    model_rows: list[dict[str, Any]] = []
    grid_rows: list[dict[str, Any]] = []
    model_fit_calls: dict[str, int] = {}
    model_predict_calls: dict[str, int] = {}
    scored_frames: dict[str, pd.DataFrame] = {}
    for model_name in MODEL_NAMES:
        scored = frame.copy()
        scored["p_up"], fits, predicts = expanding_probabilities(scored, runs, features, model_name)
        scored["p_down"] = 1.0 - scored["p_up"]
        scored_frames[model_name] = scored
        model_fit_calls[model_name] = fits
        model_predict_calls[model_name] = predicts
        scored_rows = scored[scored["p_up"].notna() & scored["outcome_up"].notna()]
        if not scored_rows.empty:
            y = scored_rows["outcome_up"].astype(int).to_numpy()
            p = scored_rows["p_up"].clip(1e-6, 1.0 - 1e-6).to_numpy()
            model_rows.append({
                "model": model_name,
                "scored_rows": int(len(scored_rows)),
                "log_loss": float(log_loss(y, p, labels=[0, 1])),
                "brier": float(brier_score_loss(y, p)),
                "mean_probability": float(np.mean(p)),
            })
        for threshold in THRESHOLDS:
            for price_cap in PRICE_CAPS:
                for spread_cap in SPREAD_CAPS:
                    events = make_events(scored, threshold, price_cap, spread_cap)
                    selection = replay_metrics(events, selection_runs, SLIPPAGES[0])
                    holdout = replay_metrics(events, holdout_runs, SLIPPAGES[0])
                    blocks_005 = [replay_metrics(events, names, SLIPPAGES[0]) for names in holdout_blocks]
                    holdout_010 = replay_metrics(events, holdout_runs, SLIPPAGES[1])
                    blocks_010 = [replay_metrics(events, names, SLIPPAGES[1]) for names in holdout_blocks]
                    grid_rows.append({
                        "model": model_name,
                        "threshold": threshold,
                        "price_cap": price_cap,
                        "spread_cap": spread_cap,
                        "signals": int(len(events)),
                        "selection_trades": selection["trades"],
                        "selection_cash": selection["ending_cash"],
                        "selection_min_cash": selection["min_cash_after_entry"],
                        "holdout_trades": holdout["trades"],
                        "holdout_cash": holdout["ending_cash"],
                        "holdout_min_cash": holdout["min_cash_after_entry"],
                        "holdout_cash_010": holdout_010["ending_cash"],
                        "holdout_min_cash_010": holdout_010["min_cash_after_entry"],
                        "holdout_min_block_cash_005": min(x["ending_cash"] for x in blocks_005),
                        "holdout_min_block_cash_010": min(x["ending_cash"] for x in blocks_010),
                        "holdout_min_block_pnl_005": min(x["net_pnl"] for x in blocks_005),
                        "holdout_min_block_pnl_010": min(x["net_pnl"] for x in blocks_010),
                    })
    grid = pd.DataFrame(grid_rows)
    eligible = grid[(grid["selection_trades"] >= 10) & (grid["selection_min_cash"] >= 0.0)]
    selected: dict[str, Any] | None = None
    if not eligible.empty:
        selected = eligible.sort_values(["selection_cash", "selection_min_cash", "selection_trades"], ascending=[False, False, False]).iloc[0].to_dict()
    gate = False
    if selected is not None:
        gate = (
            selected["holdout_trades"] >= 10
            and selected["holdout_cash"] >= 10.0
            and selected["holdout_cash_010"] >= 10.0
            and selected["holdout_min_cash"] >= 0.0
            and selected["holdout_min_cash_010"] >= 0.0
            and selected["holdout_min_block_cash_005"] >= 5.0
            and selected["holdout_min_block_cash_010"] >= 5.0
            and selected["holdout_min_block_pnl_005"] >= 0.0
            and selected["holdout_min_block_pnl_010"] >= 0.0
        )
    output.mkdir(parents=True, exist_ok=True)
    grid.to_csv(output / "grid.csv", index=False)
    pd.DataFrame(model_rows).to_csv(output / "model_metrics.csv", index=False)
    report = {
        "format": "quantml-gnb-lda-old-clean-small-cash-v1",
        "source_commit": source_commit,
        "complete_runs": len(runs),
        "usable_m1_markets": len(frame),
        "features": features,
        "models": list(MODEL_NAMES),
        "model_fit_calls": model_fit_calls,
        "model_predict_calls": model_predict_calls,
        "model_metrics": model_rows,
        "grid_cells": len(grid),
        "selection_eligible_cells": len(eligible),
        "selected_cell": selected,
        "same_ledger": True,
        "required_consecutive_resolved_fills": 10,
        "protocol": {
            "initial_cash_usd": 10.0,
            "minimum_order_usd": DEFAULT_MINIMUM_ORDER_USD,
            "minimum_order_shares": DEFAULT_MINIMUM_ORDER_SHARES,
            "minimum_order_resolution": "required_cash=max(usd_floor, share_floor*fill_price)",
            "fee_rate": 0.07,
            "settlement_plus_release_seconds": 900.0,
            "slippages": list(SLIPPAGES),
        },
        "promotion_gate": "selected cell must have >=10 holdout resolved trades, >=$10 ending cash, nonnegative minimum cash, and every holdout block >=$5 with nonnegative PnL at both slippages",
        "promotion_passed": bool(gate),
        "new_data_collected": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "paper_only": True,
        "decision": "paper_candidate_only_receipt_gate_remains_closed" if gate else "reject_no_stable_gnb_lda_cash_chain",
    }
    (output / "meta.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("format", "grid_cells", "selection_eligible_cells", "promotion_passed", "decision")}, ensure_ascii=False, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
