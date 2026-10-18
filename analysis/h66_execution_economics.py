"""H66: old-clean execution-economics gates for the H30 candidate.

The H65 diagnostic found that the two failed same-ledger cells had negative
fee-adjusted raw edge.  H66 therefore freezes four disjoint gate tracks and
evaluates every gate on selection20 and the untouched rolling holdout.  A
track is safe to run in parallel in GitHub Actions; the aggregate decision is
made only after all track outputs are available.  This is paper replay only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h55_tick_depth_prefix_audit as h55
import h57_causal_loss_window_diagnostic as h57
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CANDIDATE = "h30_move_uncertainty"
TRACK_GATES: dict[str, tuple[dict[str, Any], ...]] = {
    "raw_edge": (
        {"gate_id": "raw_edge_000", "min_raw_edge": 0.00},
        {"gate_id": "raw_edge_010", "min_raw_edge": 0.01},
        {"gate_id": "raw_edge_020", "min_raw_edge": 0.02},
        {"gate_id": "raw_edge_040", "min_raw_edge": 0.04},
    ),
    "quote_quality": (
        {"gate_id": "quote_cap_080", "max_quote_price": 0.80},
        {"gate_id": "quote_cap_075", "max_quote_price": 0.75},
        {"gate_id": "quote_cap_070", "max_quote_price": 0.70},
    ),
    "path_quality": (
        {"gate_id": "path_cap_030", "max_path_efficiency": 0.30},
        {"gate_id": "path_cap_025", "max_path_efficiency": 0.25},
        {"gate_id": "path_cap_020", "max_path_efficiency": 0.20},
    ),
    "combined": (
        {"gate_id": "raw_edge_000_quote_cap_080", "min_raw_edge": 0.00, "max_quote_price": 0.80},
        {"gate_id": "raw_edge_010_quote_cap_080", "min_raw_edge": 0.01, "max_quote_price": 0.80},
        {"gate_id": "raw_edge_020_quote_cap_080", "min_raw_edge": 0.02, "max_quote_price": 0.80},
        {"gate_id": "raw_edge_010_path_cap_030", "min_raw_edge": 0.01, "max_path_efficiency": 0.30},
        {"gate_id": "raw_edge_020_path_cap_030", "min_raw_edge": 0.02, "max_path_efficiency": 0.30},
    ),
}


def gate_mask(entries: pd.DataFrame, spec: dict[str, Any]) -> pd.Series:
    keep = entries["signal"].copy()
    if "min_raw_edge" in spec:
        keep &= entries["raw_edge"].ge(float(spec["min_raw_edge"]))
    if "max_quote_price" in spec:
        keep &= entries["quote_price"].le(float(spec["max_quote_price"]))
    if "max_quote_spread" in spec:
        keep &= entries["quote_spread"].le(float(spec["max_quote_spread"]))
    if "max_path_efficiency" in spec:
        keep &= entries["path_efficiency_first2m"].le(float(spec["max_path_efficiency"]))
    return keep.fillna(False)


def apply_gate(frame: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    out = frame.copy()
    entries = h57.entry_table(out, CANDIDATE)
    keep = gate_mask(entries, spec)
    out.loc[~keep, f"{CANDIDATE}_side"] = "skip"
    out.loc[~keep, f"{CANDIDATE}_fraction"] = 0.0
    return out


def replay(frame: pd.DataFrame, spec: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    gated = apply_gate(frame, spec)
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(
            gated, CANDIDATE, names, config["tick_size"], config["depth_floor"], config["slippage"]
        )
        rows.append({"candidate": CANDIDATE, "gate_id": spec["gate_id"], **config, **metrics})
    return rows


def evaluate_track(source_root: Path, *, source_commit: str, track: str, output: Path) -> dict[str, Any]:
    if track not in TRACK_GATES:
        raise ValueError(f"UNKNOWN_TRACK:{track}")
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
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, Any]] = []
    holdout_rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []
    entries = h57.entry_table(frame, CANDIDATE)
    for spec in TRACK_GATES[track]:
        for row in replay(frame, spec, selection_runs):
            row.update({"split": "selection20", "track": track})
            selection_rows.append(row)
        for window_index, (_start, names) in enumerate(windows, start=1):
            for row in replay(frame, spec, names):
                row.update({"split": f"holdout_window_{window_index}", "window_index": window_index, "track": track})
                holdout_rows.append(row)
        selected = gate_mask(entries, spec)
        entry_rows.append({
            "track": track,
            "gate_id": spec["gate_id"],
            "entry_rows": int(len(entries)),
            "eligible_entries": int(selected.sum()),
            "eligible_share": float(selected.mean()),
            "mean_raw_edge_eligible": float(entries.loc[selected, "raw_edge"].mean()) if selected.any() else float("nan"),
            "mean_quote_price_eligible": float(entries.loc[selected, "quote_price"].mean()) if selected.any() else float("nan"),
            "mean_quote_size_eligible": float(entries.loc[selected, "quote_size"].mean()) if selected.any() else float("nan"),
        })

    selection = pd.DataFrame(selection_rows)
    holdout = pd.DataFrame(holdout_rows)
    entry_frame = pd.DataFrame(entry_rows)
    eligible: list[dict[str, Any]] = []
    for spec in TRACK_GATES[track]:
        part = selection[selection["gate_id"].eq(spec["gate_id"])]
        if len(part) == len(CONFIGS) and bool(part["first_ten_passed"].all()) and bool((part["first_ten_fills"] == 10).all()):
            eligible.append({
                "gate_id": spec["gate_id"],
                "min_selection_cash": float(part["first_ten_ending_cash_usd"].min()),
                "selection_cash_sum": float(part["first_ten_ending_cash_usd"].sum()),
            })
    selected_gate = sorted(eligible, key=lambda x: (-x["min_selection_cash"], -x["selection_cash_sum"], x["gate_id"]))[0] if eligible else None
    summary: dict[str, Any] = {
        "format": "old-clean-h66-execution-economics-track-v1",
        "track": track,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": CANDIDATE,
        "gate_catalogue": list(TRACK_GATES[track]),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_count": len(windows),
        "eligible_selection_gates": eligible,
        "selected_gate_in_track": selected_gate,
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
    entry_frame.to_csv(output / "entry_eligibility.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--track", choices=sorted(TRACK_GATES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_track(args.source_root, source_commit=args.source_commit, track=args.track, output=args.output)
    print(json.dumps({"track": args.track, "gate_count": len(result["gate_catalogue"])}, sort_keys=True))


if __name__ == "__main__":
    main()
