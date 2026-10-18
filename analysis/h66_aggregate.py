"""Aggregate encrypted-track inputs for H66 without re-reading market data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


def aggregate(input_root: Path, output: Path) -> dict[str, Any]:
    metas = sorted(input_root.rglob("meta.json"))
    if len(metas) != 4:
        raise RuntimeError(f"H66_TRACK_COUNT_MISMATCH:{len(metas)}")
    tracks: list[str] = []
    selection_parts: list[pd.DataFrame] = []
    holdout_parts: list[pd.DataFrame] = []
    entry_parts: list[pd.DataFrame] = []
    summaries: list[dict[str, Any]] = []
    source_commits: set[str] = set()
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        track = str(meta["track"])
        tracks.append(track)
        source_commits.add(str(meta["source_commit"]))
        summaries.append(meta)
        selection_parts.append(pd.read_csv(meta_path.parent / "selection_metrics.csv"))
        holdout_parts.append(pd.read_csv(meta_path.parent / "holdout_metrics.csv"))
        entry_parts.append(pd.read_csv(meta_path.parent / "entry_eligibility.csv"))
    if set(tracks) != {"raw_edge", "quote_quality", "path_quality", "combined"}:
        raise RuntimeError(f"H66_TRACK_SET_MISMATCH:{sorted(tracks)}")
    if source_commits != {"bbda5f2050685f63153621a54641ebdfab943487"}:
        raise RuntimeError(f"H66_SOURCE_COMMITS_MISMATCH:{sorted(source_commits)}")
    selection = pd.concat(selection_parts, ignore_index=True)
    holdout = pd.concat(holdout_parts, ignore_index=True)
    entries = pd.concat(entry_parts, ignore_index=True)
    eligible: list[dict[str, Any]] = []
    for gate_id in sorted(selection["gate_id"].unique()):
        part = selection[selection["gate_id"].eq(gate_id)]
        if len(part) == 2 and bool(part["first_ten_passed"].all()) and bool((part["first_ten_fills"] == 10).all()):
            eligible.append({
                "gate_id": gate_id,
                "min_selection_cash": float(part["first_ten_ending_cash_usd"].min()),
                "selection_cash_sum": float(part["first_ten_ending_cash_usd"].sum()),
            })
    selected = sorted(eligible, key=lambda x: (-x["min_selection_cash"], -x["selection_cash_sum"], x["gate_id"]))[0] if eligible else None
    selected_holdout = holdout[holdout["gate_id"].eq((selected or {}).get("gate_id"))]
    holdout_gate = bool(
        selected
        and len(selected_holdout) == 18
        and bool(selected_holdout["first_ten_passed"].all())
        and bool((selected_holdout["first_ten_fills"] == 10).all())
    )
    gate_holdout: list[dict[str, Any]] = []
    for gate_id in sorted(holdout["gate_id"].unique()):
        part = holdout[holdout["gate_id"].eq(gate_id)]
        gate_holdout.append({
            "gate_id": gate_id,
            "cells": int(len(part)),
            "pass_cells": int(part["first_ten_passed"].sum()),
            "all_18_pass": bool(len(part) == 18 and part["first_ten_passed"].all() and (part["first_ten_fills"] == 10).all()),
            "min_holdout_cash": float(part["first_ten_ending_cash_usd"].min()) if not part.empty else None,
        })
    summary: dict[str, Any] = {
        "format": "old-clean-h66-execution-economics-aggregate-v1",
        "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
        "tracks": sorted(tracks),
        "gate_count": int(selection["gate_id"].nunique()),
        "selection_runs": 20,
        "holdout_runs": 59,
        "window_count": 9,
        "eligible_selection_gates": eligible,
        "selected_gate": selected,
        "holdout_gate": holdout_gate,
        "gate_holdout_summary": gate_holdout,
        "entry_eligibility_rows": int(len(entries)),
        "protocol": {
            "initial_cash_usd": 10.0,
            "fee_rate": 0.07,
            "cash_release_delay_seconds": 600.0,
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
        "decision": "promote_only_if_selected_gate_passes_all_18_untouched_h30_cells",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "old-clean-h66-execution-economics-sealed-v1",
        "meta": summary,
        "track_summaries": summaries,
        "selection_metrics_csv": selection.to_csv(index=False),
        "holdout_metrics_csv": holdout.to_csv(index=False),
        "entry_eligibility_csv": entries.to_csv(index=False),
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"gate_count": summary["gate_count"], "selected_gate": selected, "holdout_gate": holdout_gate}, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    aggregate(args.input_root, args.output)


if __name__ == "__main__":
    main()
