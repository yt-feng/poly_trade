"""H58: stability-first gate selection on the frozen old-clean dataset.

H57 selected an entry gate on the aggregate selection20 prefix.  That choice
was allowed to look strong after averaging across the twenty runs, yet it
failed more rolling holdout windows than the frozen baseline.  H58 keeps the
same causal gate catalogue and execution contract, but requires a gate to
pass two contiguous, predeclared ten-run selection blocks under both nominal
and stress replay before it can be selected.  The selected gate is evaluated
once on H56's nine rolling holdout windows.

This is a paper replay only.  Each selection or holdout block is a fresh
evaluation ledger; within a block the ten fills are chronological and share
one evolving cash ledger.  Blocks are never concatenated into a synthetic
Canary chain.
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

import v1_execution_walk_forward as study
import h55_tick_depth_prefix_audit as h55
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS
from h57_causal_loss_window_diagnostic import entry_table, gate_mask, apply_gate, GATE_CATALOGUE

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
SELECTION_BLOCK_SIZE = 10
SELECTION_BLOCK_COUNT = 2


def contiguous_selection_blocks(names: list[str]) -> list[tuple[int, list[str]]]:
    """Return the two predeclared contiguous ten-run selection blocks."""
    expected = SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT
    if len(names) != expected:
        raise ValueError(f"SELECTION_RUN_COUNT_MISMATCH:{len(names)}:{expected}")
    return [
        (index + 1, names[index * SELECTION_BLOCK_SIZE : (index + 1) * SELECTION_BLOCK_SIZE])
        for index in range(SELECTION_BLOCK_COUNT)
    ]


def replay(frame: pd.DataFrame, candidate: str, names: list[str], config: dict[str, Any]) -> dict[str, object]:
    metrics, _logs = h55.replay(
        apply_gate(frame, candidate, config["gate_spec"]),
        candidate,
        names,
        config["tick_size"],
        config["depth_floor"],
        config["slippage"],
    )
    return metrics


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs = test_runs[: SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT]
    holdout_runs = test_runs[SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT :]
    selection_blocks = contiguous_selection_blocks(selection_runs)
    holdout_windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, object]] = []
    selected: dict[str, dict[str, Any] | None] = {}
    for candidate in h55.CANDIDATES:
        for spec in GATE_CATALOGUE:
            for block_index, names in selection_blocks:
                for config in CONFIGS:
                    metrics = replay(
                        frame,
                        candidate,
                        names,
                        {**config, "gate_spec": spec},
                    )
                    selection_rows.append(
                        {
                            "candidate": candidate,
                            "gate_id": spec["gate_id"],
                            "selection_block": block_index,
                            "split": f"selection_block_{block_index}",
                            **config,
                            **metrics,
                        }
                    )

        part = pd.DataFrame(selection_rows)
        part = part[part["candidate"].eq(candidate)]
        eligible: list[dict[str, Any]] = []
        for spec in GATE_CATALOGUE:
            rows = part[part["gate_id"].eq(spec["gate_id"])]
            if len(rows) != len(selection_blocks) * len(CONFIGS):
                continue
            if bool(rows["first_ten_passed"].all()) and bool((rows["first_ten_fills"] == 10).all()):
                eligible.append(
                    {
                        "gate_id": spec["gate_id"],
                        "min_selection_cash": float(rows["first_ten_ending_cash_usd"].min()),
                        "selection_cash_sum": float(rows["first_ten_ending_cash_usd"].sum()),
                        "min_selection_margin": float(
                            (rows["first_ten_ending_cash_usd"] - h55.INITIAL_CASH).min()
                        ),
                    }
                )
        selected[candidate] = (
            sorted(
                eligible,
                key=lambda row: (
                    -row["min_selection_cash"],
                    -row["selection_cash_sum"],
                    row["gate_id"],
                ),
            )[0]
            if eligible
            else None
        )

    holdout_rows: list[dict[str, object]] = []
    for candidate in h55.CANDIDATES:
        chosen = selected[candidate]
        spec = next(
            (item for item in GATE_CATALOGUE if item["gate_id"] == (chosen or {}).get("gate_id")),
            None,
        )
        if spec is None:
            continue
        for window_index, (_start, names) in enumerate(holdout_windows, start=1):
            for config in CONFIGS:
                metrics = replay(frame, candidate, names, {**config, "gate_spec": spec})
                holdout_rows.append(
                    {
                        "candidate": candidate,
                        "gate_id": spec["gate_id"],
                        "split": f"holdout_window_{window_index}",
                        "window_index": window_index,
                        **config,
                        **metrics,
                    }
                )

    selection = pd.DataFrame(selection_rows)
    holdout = pd.DataFrame(holdout_rows)
    gates: dict[str, dict[str, object]] = {}
    for candidate in h55.CANDIDATES:
        chosen = selected[candidate]
        rows = holdout[holdout["candidate"].eq(candidate)] if not holdout.empty else pd.DataFrame()
        gate_pass = bool(
            chosen
            and len(rows) == len(holdout_windows) * len(CONFIGS)
            and rows["first_ten_passed"].all()
            and (rows["first_ten_fills"] == 10).all()
        )
        gates[candidate] = {
            "selected_gate": chosen,
            "rolling_holdout_gate": gate_pass,
            "holdout_rows": rows.to_dict(orient="records"),
        }

    summary = {
        "format": "old-clean-h58-robust-walk-forward-selection-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": list(h55.CANDIDATES),
        "gate_catalogue": list(GATE_CATALOGUE),
        "selection_blocks": [
            {"block_index": index, "run_count": len(names), "first_run": names[0], "last_run": names[-1]}
            for index, names in selection_blocks
        ],
        "selection_rule": (
            "eligible only if every contiguous selection block and both nominal/stress configs "
            "pass exact ten chronological fills with positive first-ten cash growth; rank by "
            "worst-block ending cash, then aggregate ending cash, then gate_id; never use holdout"
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
            "selection_block_size_runs": SELECTION_BLOCK_SIZE,
            "selection_block_count": SELECTION_BLOCK_COUNT,
        },
        "documentation_sources": h55.DOCS,
        "window_size_runs": WINDOW_SIZE,
        "window_stride_runs": WINDOW_STRIDE,
        "window_count": len(holdout_windows),
        "selection_rows": selection.to_dict(orient="records"),
        "gates": gates,
        "all_candidates_rolling_gate": bool(all(item["rolling_holdout_gate"] for item in gates.values())),
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_block_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    (output / "meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "selected": {candidate: (value or {}).get("gate_id") for candidate, value in selected.items()},
                "rolling_gates": {candidate: value["rolling_holdout_gate"] for candidate, value in gates.items()},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
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
