"""H74: favorite-side-only gate for the frozen H30 signal.

An external public BTC 5m study reports that buying the market's favorite side
is more robust than betting against the market sign.  H74 treats that as one
predeclared hypothesis and checks it on the unchanged old-clean data.  The
selection/holdout split, fee, depth, tick, cash-release and same-ledger rules
are frozen; this remains a paper replay and never sends orders.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h55_tick_depth_prefix_audit as h55
import h57_causal_loss_window_diagnostic as h57
from h56_rolling_window_stability import CONFIGS, rolling_windows

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CANDIDATE = "h30_move_uncertainty"


def favorite_mask(entries: pd.DataFrame) -> pd.Series:
    side = entries["side"]
    probability = pd.to_numeric(entries["market_probability"], errors="coerce")
    favorite = ((side == "buy_up") & probability.ge(0.5)) | (
        (side == "buy_down") & probability.le(0.5)
    )
    confidence = entries["abs_market_deviation"].ge(0.20)
    return (entries["signal"] & confidence & favorite).fillna(False)


def gated_frame(frame: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    out = frame.copy()
    out.loc[~mask, f"{CANDIDATE}_side"] = "skip"
    out.loc[~mask, f"{CANDIDATE}_fraction"] = 0.0
    return out


def replay(frame: pd.DataFrame, names: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(
            frame,
            CANDIDATE,
            names,
            config["tick_size"],
            config["depth_floor"],
            config["slippage"],
        )
        rows.append({**config, **metrics})
    return rows


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = h57.study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    entries = h57.entry_table(frame, CANDIDATE)
    mask = favorite_mask(entries)
    gated = gated_frame(frame, mask)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)
    selection = pd.DataFrame(
        [{"split": "selection20", **row} for row in replay(gated, selection_runs)]
    )
    holdout_rows: list[dict[str, Any]] = []
    for window_index, (_start, names) in enumerate(windows, start=1):
        for row in replay(gated, names):
            holdout_rows.append(
                {
                    "split": f"holdout_window_{window_index}",
                    "window_index": window_index,
                    **row,
                }
            )
    holdout = pd.DataFrame(holdout_rows)
    selection_pass = bool(
        selection["first_ten_passed"].all()
        and (selection["first_ten_fills"] == 10).all()
    )
    summary = {
        "format": "old-clean-h74-favorite-side-gate-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": CANDIDATE,
        "gate": {
            "min_abs_market_deviation": 0.20,
            "favorite_rule": "buy_up iff mid_up_prob_2m >= 0.5; buy_down iff mid_up_prob_2m <= 0.5",
            "eligible_entries": int(mask.sum()),
            "entry_rows": int(len(mask)),
            "eligible_share": float(mask.mean()),
        },
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_count": len(windows),
        "selection_pass": selection_pass,
        "selection_min_cash_usd": float(selection["first_ten_ending_cash_usd"].min()),
        "selection_cash_sum_usd": float(selection["first_ten_ending_cash_usd"].sum()),
        "holdout_pass_cells": int(holdout["first_ten_passed"].sum()),
        "holdout_total_cells": int(len(holdout)),
        "holdout_min_cash_usd": float(holdout["first_ten_ending_cash_usd"].min()),
        "holdout_gate": bool(
            len(holdout) == len(windows) * len(CONFIGS)
            and holdout["first_ten_passed"].all()
            and (holdout["first_ten_fills"] == 10).all()
        ),
        "protocol": {
            "initial_cash_usd": h55.INITIAL_CASH,
            "minimum_order_shares": h55.MINIMUM_ORDER_SHARES,
            "fee_rate": h55.FEE_RATE,
            "cash_release_delay_seconds": h55.CASH_RELEASE_DELAY_SECONDS,
            "configs": list(CONFIGS),
            "same_ledger": True,
            "required_fills": 10,
            "no_concatenation": True,
            "no_refit": True,
        },
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    (output / "meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: summary[k] for k in ("selection_pass", "holdout_pass_cells", "holdout_total_cells", "holdout_min_cash_usd", "holdout_gate")}, sort_keys=True))
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
