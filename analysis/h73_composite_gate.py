"""H73: predeclared composite entry gates on unchanged old-clean data.

H73 combines the previously frozen H30 confidence threshold with one
execution-quality condition.  The two combinations are hypotheses; the
confidence-only gate is a control.  Selection20 is evaluated before the
untouched rolling holdout, and no holdout row selects a gate.  This is a paper
replay only: it never submits orders, infers fills from public quotes, or
resets the cash ledger inside a rolling window.
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
VARIANTS: tuple[dict[str, Any], ...] = (
    {"variant": "control_confidence020", "min_abs_market_deviation": 0.20},
    {
        "variant": "confidence020_path030",
        "min_abs_market_deviation": 0.20,
        "max_path_efficiency": 0.30,
    },
    {
        "variant": "confidence020_quote075",
        "min_abs_market_deviation": 0.20,
        "max_quote_price": 0.75,
    },
)


def variant_mask(entries: pd.DataFrame, spec: dict[str, Any]) -> pd.Series:
    mask = h57.gate_mask(
        entries, {"min_abs_market_deviation": spec["min_abs_market_deviation"]}
    )
    if "max_path_efficiency" in spec:
        mask &= entries["path_efficiency_first2m"].le(
            float(spec["max_path_efficiency"])
        ).fillna(False)
    if "max_quote_price" in spec:
        mask &= entries["quote_price"].le(
            float(spec["max_quote_price"])
        ).fillna(False)
    return mask.fillna(False)


def gated_frame(frame: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    out = frame.copy()
    side = f"{CANDIDATE}_side"
    fraction = f"{CANDIDATE}_fraction"
    out.loc[~mask, side] = "skip"
    out.loc[~mask, fraction] = 0.0
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
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = h57.study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    entries = h57.entry_table(frame, CANDIDATE)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, Any]] = []
    holdout_rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        mask = variant_mask(entries, spec)
        gated = gated_frame(frame, mask)
        for row in replay(gated, selection_runs):
            selection_rows.append({"variant": spec["variant"], "split": "selection20", **row})
        for window_index, (_start, names) in enumerate(windows, start=1):
            for row in replay(gated, names):
                holdout_rows.append(
                    {
                        "variant": spec["variant"],
                        "split": f"holdout_window_{window_index}",
                        "window_index": window_index,
                        **row,
                    }
                )
        entry_rows.append(
            {
                "variant": spec["variant"],
                "entry_rows": int(len(entries)),
                "eligible_entries": int(mask.sum()),
                "eligible_share": float(mask.mean()),
            }
        )

    selection = pd.DataFrame(selection_rows)
    holdout = pd.DataFrame(holdout_rows)
    results: list[dict[str, Any]] = []
    for spec in VARIANTS:
        variant = spec["variant"]
        selected = selection[selection["variant"].eq(variant)]
        tested = holdout[holdout["variant"].eq(variant)]
        selection_pass = bool(
            selected["first_ten_passed"].all()
            and (selected["first_ten_fills"] == 10).all()
        )
        results.append(
            {
                "variant": variant,
                "selection_pass": selection_pass,
                "selection_min_cash_usd": float(selected["first_ten_ending_cash_usd"].min()),
                "selection_cash_sum_usd": float(selected["first_ten_ending_cash_usd"].sum()),
                "holdout_pass_cells": int(tested["first_ten_passed"].sum()),
                "holdout_total_cells": int(len(tested)),
                "holdout_min_cash_usd": float(tested["first_ten_ending_cash_usd"].min()),
                "holdout_gate": bool(
                    len(tested) == len(windows) * len(CONFIGS)
                    and tested["first_ten_passed"].all()
                    and (tested["first_ten_fills"] == 10).all()
                ),
            }
        )

    summary = {
        "format": "old-clean-h73-composite-gate-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": CANDIDATE,
        "variants": list(VARIANTS),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_count": len(windows),
        "selection_rule": "evaluate each predeclared variant on selection20 before reading untouched rolling holdout; no holdout selection",
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
        "entry_eligibility": entry_rows,
        "results": results,
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
    print(json.dumps({"results": results}, ensure_ascii=False, sort_keys=True))
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
