"""H65: diagnose and screen the two H64 H30 failure cells.

H64 selected a stronger market-confidence gate for H30, yet two untouched
rolling cells still finished below the initial cash after ten fills.  H65
freezes those cells before inspection, records entry-time fields for the first
ten paper fills, and tests a small finer confidence/raw-edge catalogue using
selection20 only.  It is old-clean paper evidence, never an order or fill
claim.
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
CANDIDATE = "h30_move_uncertainty"

# Frozen before reading H65 holdout outcomes.  H64's selected H30 gate was
# market_confidence_020; the finer catalogue tests whether an entry-time
# margin can remove the two predeclared losses without holdout tuning.
GATE_CATALOGUE: tuple[dict[str, Any], ...] = (
    {"gate_id": "market_confidence_020", "min_abs_market_deviation": 0.20},
    {"gate_id": "market_confidence_025", "min_abs_market_deviation": 0.25},
    {"gate_id": "market_confidence_030", "min_abs_market_deviation": 0.30},
    {"gate_id": "market_confidence_035", "min_abs_market_deviation": 0.35},
    {"gate_id": "market_confidence_040", "min_abs_market_deviation": 0.40},
    {"gate_id": "confidence020_edge020", "min_abs_market_deviation": 0.20, "min_raw_edge": 0.02},
    {"gate_id": "confidence020_edge040", "min_abs_market_deviation": 0.20, "min_raw_edge": 0.04},
    {"gate_id": "confidence025_edge020", "min_abs_market_deviation": 0.25, "min_raw_edge": 0.02},
    {"gate_id": "confidence025_edge040", "min_abs_market_deviation": 0.25, "min_raw_edge": 0.04},
    {"gate_id": "confidence030_edge040", "min_abs_market_deviation": 0.30, "min_raw_edge": 0.04},
    {"gate_id": "raw_edge_020", "min_raw_edge": 0.02},
    {"gate_id": "raw_edge_040", "min_raw_edge": 0.04},
)

H64_FAILURE_CELLS = {(1, "stress"), (8, "nominal")}


def apply_gate(frame: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    out = frame.copy()
    entries = h57.entry_table(out, CANDIDATE)
    keep = h57.gate_mask(entries, spec)
    out.loc[~keep, f"{CANDIDATE}_side"] = "skip"
    out.loc[~keep, f"{CANDIDATE}_fraction"] = 0.0
    return out


def replay(frame: pd.DataFrame, spec: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    gated = apply_gate(frame, spec)
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(
            gated,
            CANDIDATE,
            names,
            config["tick_size"],
            config["depth_floor"],
            config["slippage"],
        )
        rows.append({"candidate": CANDIDATE, "gate_id": spec["gate_id"], **config, **metrics})
    return rows


def entry_diagnostics(frame: pd.DataFrame, spec: dict[str, Any], windows: list[tuple[int, list[str]]]) -> list[dict[str, Any]]:
    gated = apply_gate(frame, spec)
    entries = h57.entry_table(frame, CANDIDATE).drop_duplicates(["run_name", "market_id"])
    entries = entries.set_index(["run_name", "market_id"], drop=False)
    out: list[dict[str, Any]] = []
    for window_index, (_start, names) in enumerate(windows, start=1):
        for config in CONFIGS:
            metrics, logs = h55.replay(
                gated,
                CANDIDATE,
                names,
                config["tick_size"],
                config["depth_floor"],
                config["slippage"],
            )
            if logs.empty:
                continue
            first = logs.sort_values("first_quote_ts", kind="stable").head(10)
            failure_cell = (window_index, config["label"]) in H64_FAILURE_CELLS
            for fill_index, row in enumerate(first.itertuples(index=False), start=1):
                key = (str(row.run_name), str(row.market_id))
                feature = entries.loc[key] if key in entries.index else pd.Series(dtype=object)
                out.append(
                    {
                        "candidate": CANDIDATE,
                        "gate_id": spec["gate_id"],
                        "window_index": window_index,
                        "config_label": config["label"],
                        "failure_cell_predeclared": failure_cell,
                        "fill_index": fill_index,
                        "run_name": str(row.run_name),
                        "market_id": str(row.market_id),
                        "side": str(row.side),
                        "entry_ts": str(row.first_quote_ts),
                        "filled_cost": float(row.filled_cost),
                        "fee_usd": float(row.fee_usd),
                        "pnl_usd": float(row.pnl_usd),
                        "market_confidence": float(feature.get("abs_market_deviation", float("nan"))),
                        "raw_edge": float(feature.get("raw_edge", float("nan"))),
                        "quote_price": float(feature.get("quote_price", float("nan"))),
                        "quote_spread": float(feature.get("quote_spread", float("nan"))),
                        "quote_size": float(feature.get("quote_size", float("nan"))),
                        "path_efficiency_first2m": float(feature.get("path_efficiency_first2m", float("nan"))),
                        "session_et": feature.get("session_et"),
                        "diagnostic_only": True,
                    }
                )
    return out


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
    for spec in GATE_CATALOGUE:
        for row in replay(frame, spec, selection_runs):
            row["split"] = "selection20"
            selection_rows.append(row)
    selection = pd.DataFrame(selection_rows)
    eligible: list[dict[str, Any]] = []
    for spec in GATE_CATALOGUE:
        rows_for_gate = selection[selection["gate_id"].eq(spec["gate_id"])]
        if len(rows_for_gate) == len(CONFIGS) and bool(rows_for_gate["first_ten_passed"].all()) and bool(
            (rows_for_gate["first_ten_fills"] == 10).all()
        ):
            eligible.append(
                {
                    "gate_id": spec["gate_id"],
                    "min_selection_cash": float(rows_for_gate["first_ten_ending_cash_usd"].min()),
                    "selection_cash_sum": float(rows_for_gate["first_ten_ending_cash_usd"].sum()),
                }
            )
    selected = sorted(eligible, key=lambda x: (-x["min_selection_cash"], -x["selection_cash_sum"], x["gate_id"]))[0] if eligible else None
    holdout_rows: list[dict[str, Any]] = []
    if selected:
        spec = next(x for x in GATE_CATALOGUE if x["gate_id"] == selected["gate_id"])
        for window_index, (_start, names) in enumerate(windows, start=1):
            for row in replay(frame, spec, names):
                row["split"] = f"holdout_window_{window_index}"
                row["window_index"] = window_index
                holdout_rows.append(row)
    holdout = pd.DataFrame(holdout_rows)
    holdout_gate = bool(
        selected
        and len(holdout) == len(windows) * len(CONFIGS)
        and holdout["first_ten_passed"].all()
        and (holdout["first_ten_fills"] == 10).all()
    )
    diagnostics = entry_diagnostics(frame, next(x for x in GATE_CATALOGUE if x["gate_id"] == (selected or {"gate_id": "market_confidence_020"})["gate_id"]), windows)
    diag_frame = pd.DataFrame(diagnostics)
    group_summary = []
    if not diag_frame.empty:
        for failure, group in diag_frame.groupby("failure_cell_predeclared", dropna=False):
            group_summary.append(
                {
                    "failure_cell_predeclared": bool(failure),
                    "rows": int(len(group)),
                    "mean_market_confidence": float(group["market_confidence"].mean()),
                    "mean_raw_edge": float(group["raw_edge"].mean()),
                    "mean_quote_price": float(group["quote_price"].mean()),
                    "mean_quote_spread": float(group["quote_spread"].mean()),
                    "mean_quote_size": float(group["quote_size"].mean()),
                    "mean_path_efficiency": float(group["path_efficiency_first2m"].mean()),
                    "mean_pnl_usd": float(group["pnl_usd"].mean()),
                }
            )
    summary: dict[str, Any] = {
        "format": "old-clean-h65-high-confidence-failure-diagnostic-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": CANDIDATE,
        "gate_catalogue": list(GATE_CATALOGUE),
        "predeclared_h64_failure_cells": [{"window_index": w, "config_label": c} for w, c in sorted(H64_FAILURE_CELLS)],
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_size_runs": WINDOW_SIZE,
        "window_stride_runs": WINDOW_STRIDE,
        "window_count": len(windows),
        "selection_rule": "select one gate only from selection20 when both nominal and stress selection cells pass exact ten chronological fills with positive first-ten cash growth; never use holdout for selection",
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
        "eligible_selection_gates": eligible,
        "selected_gate": selected,
        "holdout_gate": holdout_gate,
        "diagnostic_group_summary": group_summary,
        "diagnostic_rows": len(diagnostics),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "decision": "promote_only_if_selected_gate_passes_all_18_untouched_h30_cells",
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    diag_frame.to_csv(output / "failure_diagnostics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selected_gate": (selected or {}).get("gate_id"), "holdout_gate": holdout_gate, "diagnostic_rows": len(diagnostics)}, ensure_ascii=False, sort_keys=True))
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
