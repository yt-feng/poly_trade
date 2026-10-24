"""Pre-registered QuantML divergence-gate challenger on immutable old data.

The rule is derived from the reviewed QuantML event-independence material: a
large, timestamp-correct disagreement between the BTC short-horizon move and
the market-implied probability move is treated as an event filter.  The
candidate trades in the BTC-move direction only when the two signals disagree
and the order-book side/depth gate is satisfied by the frozen executor.

This module is paper-only.  It reuses the existing causal small-cash simulator
and never enables orders or counts a canary fill.
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
BLOCK_SIZES = (12, 12, 12, 12, 11)
SLIPPAGES = (0.005, 0.010, 0.015)
STRATEGY = "quantml_btc_probability_divergence_gate"


def _num(frame: pd.DataFrame, name: str) -> pd.Series:
    value = frame.get(name)
    if value is None:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(value, errors="coerce").replace([np.inf, -np.inf], np.nan)


def add_candidate(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    btc = _num(out, "btc_move_2m") / 50.0
    implied = _num(out, "mid_up_prob_change_2m") / 0.05
    uncertainty = (1.0 - 2.0 * (_num(out, "mid_up_prob_2m").fillna(0.5) - 0.5).abs()).clip(0.0, 1.0)

    divergence = btc - implied
    btc_leads = btc.abs().ge(0.05) & (np.sign(btc) == np.sign(divergence))
    valid = btc_leads & divergence.notna() & (divergence.abs() >= 0.05)
    factor = (np.sign(btc) * divergence.abs() * (0.5 + 0.5 * uncertainty)).where(valid)

    out[STRATEGY] = factor
    out[f"{STRATEGY}_consensus"] = valid
    out[f"{STRATEGY}_side"] = "skip"
    out[f"{STRATEGY}_minute"] = -1
    out[f"{STRATEGY}_fraction"] = 0.04
    out[f"{STRATEGY}_component"] = "btc_move_vs_market_implied_probability_divergence"
    return out


def apply_thresholds(frame: pd.DataFrame, low: float, high: float) -> pd.DataFrame:
    out = frame.copy()
    factor = pd.to_numeric(out[STRATEGY], errors="coerce")
    valid = out[f"{STRATEGY}_consensus"].fillna(False).astype(bool)
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[valid & factor.ge(high)] = "buy_up"
    side.loc[valid & factor.le(low)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out[f"{STRATEGY}_minute"] = np.where(side.eq("skip"), -1, 2)
    return out


def _blocks(test_runs: list[str]) -> list[list[str]]:
    if len(test_runs) != sum(BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_HOLDOUT59")
    result, cursor = [], 0
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
    values = pd.to_numeric(selection.loc[selection[f"{STRATEGY}_consensus"], STRATEGY], errors="coerce").dropna()
    if len(values) < 20:
        raise RuntimeError("INSUFFICIENT_SELECTION_DIVERGENCE_ROWS")
    low = float(values.quantile(0.20))
    high = float(values.quantile(0.80))
    if not np.isfinite(low) or not np.isfinite(high) or low >= high:
        raise RuntimeError("INVALID_FIXED_DIVERGENCE_THRESHOLDS")
    features = apply_thresholds(features, low, high)

    rows: list[dict[str, object]] = []
    for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs)):
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
    for index, names in enumerate(_blocks(holdout_runs), start=1):
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
            rows.append({"variant": STRATEGY, "split": f"block{index}", "slippage": slippage, **metrics})

    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    holdout = result[(result["split"] == "holdout59") & (result["slippage"] == SLIPPAGES[0])].iloc[0]
    blocks = result[result["split"].str.startswith("block")]
    gate = bool(
        int(holdout["trades"]) >= 10
        and (blocks["ending_cash"] >= 5.0).all()
        and (blocks["net_return"] >= 0.0).all()
    )
    meta: dict[str, object] = {
        "format": "old-data-causal-quantml-divergence-gate-v1",
        "hypothesis_id": "QML-P5-intraday-index-divergence-gate-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_sizes": list(BLOCK_SIZES),
        "factor": "sign(btc_move_2m/50)*abs(btc_move_2m/50-mid_up_prob_change_2m/0.05)*(0.5+0.5*uncertainty)",
        "fixed_event_gate": "abs(btc_move_2m/50)>=0.05 and sign(btc_move_2m)==sign(divergence) and abs(divergence)>=0.05",
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
            "minimum_block_ending_cash": float(blocks["ending_cash"].min()),
            "minimum_block_net_return": float(blocks["net_return"].min()),
            "gate_passed": gate,
        },
        "decision": "promote_only_if_gate_passes",
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "paper_only": True,
        "evidence_boundary": "Historical old-data paper replay; no order acceptance, settlement, reusable cash receipt or canary fill.",
        "source_basis": "Agent-reviewed QuantML intraday independence/divergence report; analogy only, not direct BTC5m evidence.",
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
