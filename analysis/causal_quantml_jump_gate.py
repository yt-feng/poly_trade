"""Cloud-only old-clean ablation for the pre-registered jump gate.

This entrypoint keeps the existing quantML size-imbalance candidate frozen and
adds one fixed, causal jump gate.  The gate is computed from the BTC final-price
path observed before the minute-2 entry: a bipower-variation scale is used to
standardize the largest pre-entry return, and markets with ``abs(z) > 3`` are
skipped.  The threshold is fixed before reading the holdout and is not tuned.

The result is paper evidence only.  It never enables orders or counts a canary.
All fitting, replay and aggregation run inside GitHub Actions/AWS; local use is
limited to syntax checks.
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

import causal_quantml_small_cash as baseline


EXPECTED_COMPLETE_RUNS = baseline.EXPECTED_COMPLETE_RUNS
SELECTION_RUNS = baseline.SELECTION_RUNS
BLOCK_SIZES = baseline.BLOCK_SIZES
SLIPPAGES = baseline.SLIPPAGES
BASE_STRATEGY = baseline.STRATEGY
GATED_STRATEGY = "quantml_jump_gate"
JUMP_Z_THRESHOLD = 3.0
MIN_RETURNS_FOR_GATE = 5


def _jump_z(path: pd.Series) -> float:
    """Return a fixed pre-entry jump proxy, or NaN when evidence is sparse."""
    values = pd.to_numeric(path, errors="coerce").dropna().to_numpy(dtype=float)
    returns = np.diff(values)
    returns = returns[np.isfinite(returns)]
    if len(returns) < MIN_RETURNS_FOR_GATE:
        return float("nan")
    abs_returns = np.abs(returns)
    # Bipower variation is robust to one isolated jump.  The small floor keeps
    # a flat path from creating an infinite gate value.
    products = abs_returns[1:] * abs_returns[:-1]
    bv = float(np.pi / 2.0 * np.mean(products)) if len(products) else 0.0
    scale = float(np.sqrt(max(bv, 1e-12)))
    return float(np.max(abs_returns) / scale)


def _jump_frame(raw: pd.DataFrame, complete_runs: list[str]) -> pd.DataFrame:
    """Compute entry-time jump evidence without using outcomes or post-entry rows."""
    q = raw[raw["run_name"].astype(str).isin(complete_runs)].copy()
    q["ts_utc"] = pd.to_datetime(q["ts_utc"], utc=True, errors="coerce")
    rows: list[dict[str, object]] = []
    for market_id, group in q.groupby("market_id", dropna=False):
        g = group.sort_values("ts_utc")
        if g.empty:
            continue
        first = g["ts_utc"].min()
        pre_entry = g[g["ts_utc"] <= first + pd.Timedelta(minutes=2)]
        z = _jump_z(pre_entry["final_price"])
        rows.append({
            "market_id": market_id,
            "jump_z_2m": z,
            "jump_gate_pass_2m": bool(np.isfinite(z) and abs(z) <= JUMP_Z_THRESHOLD),
            "jump_gate_evidence_sufficient": bool(np.isfinite(z)),
        })
    return pd.DataFrame(rows)


def _blocks(test_runs: list[str]) -> list[list[str]]:
    if len(test_runs) != sum(BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_HOLDOUT59")
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

    features, _coverage, runs = baseline.study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")

    raw, _ = baseline.study.base.read_all_monthly_runs(source_root)
    raw = baseline.study.base.prepare_quotes(raw)
    jump = _jump_frame(raw, runs)
    if jump.empty:
        raise RuntimeError("NO_PRE_ENTRY_JUMP_EVIDENCE")
    features = features.merge(jump, on="market_id", how="left", validate="one_to_one")
    features = baseline.add_candidate(features)

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
    features = baseline.apply_thresholds(features, low, high)

    features[f"{GATED_STRATEGY}_side"] = features[f"{BASE_STRATEGY}_side"]
    features[f"{GATED_STRATEGY}_minute"] = features[f"{BASE_STRATEGY}_minute"]
    features[f"{GATED_STRATEGY}_fraction"] = features[f"{BASE_STRATEGY}_fraction"]
    features[f"{GATED_STRATEGY}_component"] = "jump_gate_3sigma"
    blocked = ~features["jump_gate_pass_2m"].fillna(False)
    features.loc[blocked, f"{GATED_STRATEGY}_side"] = "skip"
    features.loc[blocked, f"{GATED_STRATEGY}_minute"] = -1
    features.loc[blocked, f"{GATED_STRATEGY}_fraction"] = 0.0

    rows: list[dict[str, object]] = []
    for split, names in [("selection20", selection_runs), ("holdout59", holdout_runs)]:
        for slippage in SLIPPAGES:
            subset = features[features["run_name"].isin(names)]
            for variant in (BASE_STRATEGY, GATED_STRATEGY):
                _, metrics = baseline.simulate_cash_locked(
                    subset, variant, baseline.study.ExecutionConfig(fixed_slippage=slippage),
                    10.0, 5.0, 600.0, cooldown_events=0, capture_logs=False,
                )
                rows.append({"variant": variant, "split": split, "slippage": slippage, **metrics})
    for block_index, names in enumerate(_blocks(holdout_runs), start=1):
        for slippage in SLIPPAGES:
            subset = features[features["run_name"].isin(names)]
            for variant in (BASE_STRATEGY, GATED_STRATEGY):
                _, metrics = baseline.simulate_cash_locked(
                    subset, variant, baseline.study.ExecutionConfig(fixed_slippage=slippage),
                    10.0, 5.0, 600.0, cooldown_events=0, capture_logs=False,
                )
                rows.append({"variant": variant, "split": f"block{block_index}", "slippage": slippage, **metrics})

    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    base_hold = result[(result["variant"] == BASE_STRATEGY) & (result["split"] == "holdout59") & (result["slippage"] == SLIPPAGES[0])].iloc[0]
    gate_hold = result[(result["variant"] == GATED_STRATEGY) & (result["split"] == "holdout59") & (result["slippage"] == SLIPPAGES[0])].iloc[0]
    gate_blocks = result[(result["variant"] == GATED_STRATEGY) & result["split"].str.startswith("block")]
    base_blocks = result[(result["variant"] == BASE_STRATEGY) & result["split"].str.startswith("block")]
    gate_passed = bool(
        int(gate_hold["trades"]) >= 10
        and (gate_blocks["ending_cash"] >= 5.0).all()
        and (gate_blocks["net_return"] >= 0.0).all()
        and float(gate_hold["ending_cash"]) > float(base_hold["ending_cash"])
    )
    meta: dict[str, object] = {
        "format": "old-data-causal-quantml-jump-gate-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "jump_statistic": "max absolute pre-entry final_price return divided by bipower-variation scale",
        "jump_z_threshold": JUMP_Z_THRESHOLD,
        "minimum_pre_entry_returns": MIN_RETURNS_FOR_GATE,
        "thresholds": {"selection_quantiles": [0.20, 0.80], "low": low, "high": high},
        "protocol": {
            "initial_cash": 10.0, "minimum_order_notional": 5.0, "fee_rate": 0.07,
            "settlement_seconds": 300.0, "cash_release_delay_seconds": 600.0,
            "slippages": list(SLIPPAGES), "entry_minute": 2, "delay_seconds": 4,
        },
        "summary": {
            "baseline_holdout_trades": int(base_hold["trades"]),
            "gated_holdout_trades": int(gate_hold["trades"]),
            "baseline_holdout_ending_cash": float(base_hold["ending_cash"]),
            "gated_holdout_ending_cash": float(gate_hold["ending_cash"]),
            "gated_minimum_block_ending_cash": float(gate_blocks["ending_cash"].min()),
            "gated_minimum_block_net_return": float(gate_blocks["net_return"].min()),
            "baseline_minimum_block_ending_cash": float(base_blocks["ending_cash"].min()),
            "gate_passed": gate_passed,
        },
        "decision": "promote_only_if_fixed_gate_passes",
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "Historical old-data paper replay; no order acceptance, settlement, reusable cash receipt or canary fill.",
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
