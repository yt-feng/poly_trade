"""H64: predeclared high-confidence gates on the unchanged old-clean data.

H57 only tested market-confidence gates through 0.15.  H64 extends that
catalogue to higher entry-time confidence and a small raw-edge grid.  A gate
is selected from the first 20 test runs only when every selection/config cell
passes the exact ten-fill positive-growth prefix.  The selected gate is then
evaluated once on the nine untouched rolling holdout windows.

This is a historical paper replay.  It never submits orders, infers fills
from quotes, or resets the cash ledger within a rolling window.
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
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"

# The grid is frozen before looking at holdout rows.  The 0.10/0.15 entries
# are retained as controls so an apparent improvement is comparable with H57.
GATE_CATALOGUE: tuple[dict[str, Any], ...] = (
    {"gate_id": "baseline", "description": "no additional entry gate"},
    {"gate_id": "market_confidence_010", "min_abs_market_deviation": 0.10},
    {"gate_id": "market_confidence_015", "min_abs_market_deviation": 0.15},
    {"gate_id": "market_confidence_020", "min_abs_market_deviation": 0.20},
    {"gate_id": "market_confidence_025", "min_abs_market_deviation": 0.25},
    {"gate_id": "market_confidence_030", "min_abs_market_deviation": 0.30},
    {"gate_id": "market_confidence_035", "min_abs_market_deviation": 0.35},
    {"gate_id": "market_confidence_040", "min_abs_market_deviation": 0.40},
    {"gate_id": "raw_edge_000", "min_raw_edge": 0.00},
    {"gate_id": "raw_edge_020", "min_raw_edge": 0.02},
    {"gate_id": "raw_edge_040", "min_raw_edge": 0.04},
    {"gate_id": "raw_edge_060", "min_raw_edge": 0.06},
    {"gate_id": "confidence025_edge020", "min_abs_market_deviation": 0.25, "min_raw_edge": 0.02},
    {"gate_id": "confidence030_edge040", "min_abs_market_deviation": 0.30, "min_raw_edge": 0.04},
)


def apply_gate(frame: pd.DataFrame, candidate: str, spec: dict[str, Any]) -> pd.DataFrame:
    out = frame.copy()
    entries = h57.entry_table(out, candidate)
    keep = h57.gate_mask(entries, spec)
    out.loc[~keep, f"{candidate}_side"] = "skip"
    out.loc[~keep, f"{candidate}_fraction"] = 0.0
    return out


def replay(frame: pd.DataFrame, candidate: str, spec: dict[str, Any], names: list[str]) -> dict[str, Any]:
    gated = apply_gate(frame, candidate, spec)
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(
            gated,
            candidate,
            names,
            config["tick_size"],
            config["depth_floor"],
            config["slippage"],
        )
        rows.append({"candidate": candidate, "gate_id": spec["gate_id"], **config, **metrics})
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
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, Any]] = []
    for candidate in h55.CANDIDATES:
        for spec in GATE_CATALOGUE:
            for row in replay(frame, candidate, spec, selection_runs):
                row["split"] = "selection20"
                selection_rows.append(row)
    selection = pd.DataFrame(selection_rows)

    selected: dict[str, dict[str, Any] | None] = {}
    eligible_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for candidate in h55.CANDIDATES:
        eligible: list[dict[str, Any]] = []
        part = selection[selection["candidate"].eq(candidate)]
        for spec in GATE_CATALOGUE:
            rows_for_gate = part[part["gate_id"].eq(spec["gate_id"])]
            if len(rows_for_gate) != len(CONFIGS):
                continue
            if bool(rows_for_gate["first_ten_passed"].all()) and bool(
                (rows_for_gate["first_ten_fills"] == 10).all()
            ):
                eligible.append(
                    {
                        "gate_id": spec["gate_id"],
                        "min_selection_cash": float(rows_for_gate["first_ten_ending_cash_usd"].min()),
                        "selection_cash_sum": float(rows_for_gate["first_ten_ending_cash_usd"].sum()),
                    }
                )
        eligible_by_candidate[candidate] = eligible
        selected[candidate] = (
            sorted(eligible, key=lambda x: (-x["min_selection_cash"], -x["selection_cash_sum"], x["gate_id"]))[0]
            if eligible
            else None
        )

    holdout_rows: list[dict[str, Any]] = []
    for candidate in h55.CANDIDATES:
        chosen = selected[candidate]
        if not chosen:
            continue
        spec = next(x for x in GATE_CATALOGUE if x["gate_id"] == chosen["gate_id"])
        for window_index, (_start, names) in enumerate(windows, start=1):
            for row in replay(frame, candidate, spec, names):
                row["split"] = f"holdout_window_{window_index}"
                row["window_index"] = window_index
                holdout_rows.append(row)
    holdout = pd.DataFrame(holdout_rows)

    gates: dict[str, dict[str, Any]] = {}
    for candidate in h55.CANDIDATES:
        chosen = selected[candidate]
        rows_for_candidate = holdout[holdout["candidate"].eq(candidate)] if not holdout.empty else pd.DataFrame()
        passed = bool(
            chosen
            and len(rows_for_candidate) == len(windows) * len(CONFIGS)
            and rows_for_candidate["first_ten_passed"].all()
            and (rows_for_candidate["first_ten_fills"] == 10).all()
        )
        gates[candidate] = {
            "selected_gate": chosen,
            "rolling_holdout_gate": passed,
            "holdout_pass_cells": int(rows_for_candidate["first_ten_passed"].sum()) if not rows_for_candidate.empty else 0,
            "holdout_total_cells": int(len(rows_for_candidate)),
            "holdout_rows": rows_for_candidate.to_dict(orient="records"),
        }

    summary: dict[str, Any] = {
        "format": "old-clean-h64-high-confidence-gate-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(h55.CANDIDATES),
        "gate_catalogue": list(GATE_CATALOGUE),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_size_runs": WINDOW_SIZE,
        "window_stride_runs": WINDOW_STRIDE,
        "window_count": len(windows),
        "selection_rule": "select one predeclared gate per candidate only when every nominal/stress selection cell passes exact ten chronological fills with positive first-ten cash growth; never use holdout for selection",
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
        "eligible_selection_gates": eligible_by_candidate,
        "selected_gates": {k: v for k, v in selected.items()},
        "gates": gates,
        "all_candidates_rolling_gate": bool(all(v["rolling_holdout_gate"] for v in gates.values())),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "decision": "promote_only_if_every_selected_candidate_passes_all_untouched_rolling_holdout_cells",
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selected": {k: (v or {}).get("gate_id") for k, v in selected.items()}, "rolling_gates": {k: v["rolling_holdout_gate"] for k, v in gates.items()}}, ensure_ascii=False, sort_keys=True))
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
