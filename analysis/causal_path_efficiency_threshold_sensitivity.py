"""Predeclared old-data threshold challenger for the frozen path filter.

The current paper candidate uses ``path_efficiency_first2m >= 0.50``.  This
diagnostic tests one fixed challenger, ``>= 0.55``, on the same chronological
selection/holdout split and cash-lock protocol.  It never selects the better
threshold from the holdout and does not alter the frozen candidate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked


STRATEGY = "v1_adaptive_mix"
THRESHOLDS = (0.50, 0.55)
SLIPPAGES = (0.005, 0.010, 0.015)
BLOCK_SIZES = (16, 16, 16, 16, 15)
DELAY_SECONDS = 600.0


def gated(features: pd.DataFrame, threshold: float) -> pd.DataFrame:
    out = features.copy()
    keep = pd.to_numeric(out["path_efficiency_first2m"], errors="coerce").ge(threshold)
    out.loc[~keep, f"{STRATEGY}_side"] = "skip"
    out.loc[~keep, f"{STRATEGY}_minute"] = -1
    out.loc[~keep, f"{STRATEGY}_fraction"] = 0.0
    return out


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(test_runs[cursor : cursor + size])
        cursor += size

    rows: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        frame = gated(features, threshold)
        splits = [("selection20", selection_runs), ("holdout59", holdout_runs)]
        splits.extend((f"block{index}", names) for index, names in enumerate(blocks, 1))
        for split, names in splits:
            for slippage in SLIPPAGES:
                _logs, metrics = simulate_cash_locked(
                    frame[frame["run_name"].isin(names)],
                    STRATEGY,
                    study.ExecutionConfig(fixed_slippage=slippage),
                    10.0,
                    5.0,
                    DELAY_SECONDS,
                    cooldown_events=0,
                    capture_logs=False,
                )
                rows.append(
                    {
                        "threshold": threshold,
                        "split": split,
                        "slippage": slippage,
                        **metrics,
                    }
                )
    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)

    summary: dict[str, dict[str, object]] = {}
    for threshold in THRESHOLDS:
        selected = result[result["threshold"].eq(threshold)]
        holdout = selected[
            selected["split"].eq("holdout59") & selected["slippage"].eq(SLIPPAGES[0])
        ].iloc[0]
        blocks_result = selected[selected["split"].str.startswith("block")]
        worst = blocks_result.loc[blocks_result["ending_cash"].idxmin()]
        summary[str(threshold)] = {
            "holdout_trades": int(holdout["trades"]),
            "holdout_ending_cash_005": float(holdout["ending_cash"]),
            "minimum_block_ending_cash": float(worst["ending_cash"]),
            "minimum_block_cell": {
                "split": str(worst["split"]),
                "slippage": float(worst["slippage"]),
                "trades": int(worst["trades"]),
            },
            "gate_passed": bool(
                int(holdout["trades"]) >= 10
                and (blocks_result["ending_cash"] >= 5.0).all()
                and (blocks_result["net_return"] >= 0.0).all()
            ),
        }
    meta = {
        "format": "old-data-causal-path-efficiency-threshold-sensitivity-v1",
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "strategy": STRATEGY,
        "predeclared_thresholds": list(THRESHOLDS),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_sizes": list(BLOCK_SIZES),
        "protocol": {
            "initial_cash": 10.0,
            "minimum_order_notional": 5.0,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "fill_ratio": 0.25,
            "cooldown_events": 0,
        },
        "hypothesis": "Raising path efficiency from 0.50 to 0.55 improves the weak-block cash floor enough to justify lower trade frequency.",
        "summary": summary,
        "challenger_gate_passed": summary["0.55"]["gate_passed"],
        "frozen_baseline_gate_passed": summary["0.5"]["gate_passed"],
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "candidate_promoted_to_canary": False,
        "decision": "reject_threshold_055_keep_threshold_050_frozen",
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()

