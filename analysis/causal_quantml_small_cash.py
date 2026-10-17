"""Causal small-cash audit for one predeclared quantML-style interaction.

The candidate is fixed before the holdout replay: two-minute signed size
imbalance multiplied by price uncertainty.  The sign chooses Up or Down and
the threshold is the 80/20 quantile measured on the first 20 test runs only.
The holdout is evaluated with the same $10/$5, 7% taker-fee, five-minute
settlement plus 600-second cash-release contract used by the frozen baseline.

This is paper evidence only.  It never enables an order or counts a canary.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked


EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUNS = 20
BLOCK_SIZES = (16, 16, 16, 16, 15)
SLIPPAGES = (0.005, 0.010, 0.015)
STRATEGY = "quantml_size_imbalance_uncertainty"


def add_candidate(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    uncertainty = (
        1.0 - (pd.to_numeric(out["mid_up_prob_2m"], errors="coerce").fillna(0.5) - 0.5).abs() * 2.0
    ).clip(0.0, 1.0)
    out["quantml_size_imbalance_uncertainty"] = (
        pd.to_numeric(out["size_imbalance_updown_2m"], errors="coerce") * uncertainty
    )
    out[f"{STRATEGY}_side"] = "skip"
    out[f"{STRATEGY}_minute"] = -1
    out[f"{STRATEGY}_fraction"] = 0.04
    out[f"{STRATEGY}_component"] = "quantml_size_imbalance_uncertainty"
    return out


def apply_thresholds(
    frame: pd.DataFrame, low: float, high: float
) -> pd.DataFrame:
    out = frame.copy()
    factor = pd.to_numeric(out["quantml_size_imbalance_uncertainty"], errors="coerce")
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[factor.ge(high)] = "buy_up"
    side.loc[factor.le(low)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out.loc[side.eq("skip"), f"{STRATEGY}_minute"] = -1
    out.loc[side.ne("skip"), f"{STRATEGY}_minute"] = 2
    return out


def _blocks(test_runs: list[str]) -> list[list[str]]:
    result: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        result.append(test_runs[cursor : cursor + size])
        cursor += size
    return result


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")

    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    features = add_candidate(features)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    selection = features[features["run_name"].isin(selection_runs)]
    factor = pd.to_numeric(selection["quantml_size_imbalance_uncertainty"], errors="coerce").dropna()
    if len(factor) < 20:
        raise RuntimeError("INSUFFICIENT_SELECTION_FACTOR_ROWS")
    low = float(factor.quantile(0.20))
    high = float(factor.quantile(0.80))
    if not np.isfinite(low) or not np.isfinite(high) or low >= high:
        raise RuntimeError("INVALID_FIXED_QUANTILE_THRESHOLDS")
    features = apply_thresholds(features, low, high)

    rows: list[dict[str, object]] = []
    for split, names in [("selection20", selection_runs), ("holdout59", holdout_runs)]:
        for slippage in SLIPPAGES:
            _, metrics = simulate_cash_locked(
                features[features["run_name"].isin(names)],
                STRATEGY,
                study.ExecutionConfig(fixed_slippage=slippage),
                10.0,
                5.0,
                600.0,
                cooldown_events=0,
                capture_logs=False,
            )
            rows.append({"variant": STRATEGY, "split": split, "slippage": slippage, **metrics})
    blocks = _blocks(test_runs)
    for block_index, names in enumerate(blocks, start=1):
        for slippage in SLIPPAGES:
            _, metrics = simulate_cash_locked(
                features[features["run_name"].isin(names)],
                STRATEGY,
                study.ExecutionConfig(fixed_slippage=slippage),
                10.0,
                5.0,
                600.0,
                cooldown_events=0,
                capture_logs=False,
            )
            rows.append({"variant": STRATEGY, "split": f"block{block_index}", "slippage": slippage, **metrics})

    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    holdout = result[(result["split"] == "holdout59") & (result["slippage"] == SLIPPAGES[0])].iloc[0]
    block_rows = result[result["split"].str.startswith("block")]
    gate = bool(
        int(holdout["trades"]) >= 10
        and (block_rows["ending_cash"] >= 5.0).all()
        and (block_rows["net_return"] >= 0.0).all()
    )
    meta: dict[str, object] = {
        "format": "old-data-causal-quantml-small-cash-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_sizes": list(BLOCK_SIZES),
        "hypothesis": "Two-minute size imbalance is more useful when the market-implied probability is uncertain; fixed high/low tails choose Up/Down.",
        "factor": "size_imbalance_updown_2m * (1 - 2*abs(mid_up_prob_2m - 0.5))",
        "thresholds": {"selection_quantiles": [0.20, 0.80], "low": low, "high": high},
        "protocol": {
            "initial_cash": 10.0,
            "minimum_order_notional": 5.0,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": 600.0,
            "slippages": list(SLIPPAGES),
            "entry_minute": 2,
            "delay_seconds": 4,
            "cooldown_events": 0,
        },
        "summary": {
            "holdout_trades": int(holdout["trades"]),
            "holdout_ending_cash_005": float(holdout["ending_cash"]),
            "minimum_block_ending_cash": float(block_rows["ending_cash"].min()),
            "minimum_block_net_return": float(block_rows["net_return"].min()),
            "gate_passed": gate,
        },
        "decision": "promote_only_if_gate_passes",
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "Historical old-data paper replay; no order acceptance, settlement, reusable cash receipt or canary fill.",
        "source_notes": [
            "https://arxiv.org/abs/2604.24366",
            "https://arxiv.org/abs/2607.26245",
        ],
    }
    (output / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.source_root, args.output, args.source_commit), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
