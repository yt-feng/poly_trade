"""Pre-registered QuantML breadth-consensus challenger on immutable old data.

This is a paper-only rule derived from one reviewed QuantML report.  It uses
only as-of <=2 minute BTC/market fields already present in the clean snapshot:
two order-book imbalance proxies, 1m/2m directional moves, and the 2m
probability move.  A trade is admitted only when the two short horizons and
the imbalance proxy agree; disagreement is the fixed false-breakout veto.
Thresholds are learned from selection20 only and never retuned on holdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import causal_quantml_small_cash as baseline


STRATEGY = "quantml_breadth_consensus_reversal"
EXPECTED_COMPLETE_RUNS = baseline.EXPECTED_COMPLETE_RUNS
SELECTION_RUNS = baseline.SELECTION_RUNS
BLOCK_SIZES = baseline.BLOCK_SIZES
SLIPPAGES = baseline.SLIPPAGES


def _num(frame: pd.DataFrame, name: str) -> pd.Series:
    value = frame.get(name)
    if value is None:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(value, errors="coerce").replace([np.inf, -np.inf], np.nan)


def add_candidate(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    buy_imb = _num(out, "size_imbalance_updown_2m")
    sell_imb = _num(out, "sell_size_imbalance_updown_2m")
    breadth_proxy = 0.5 * (buy_imb + sell_imb)
    move_1m = _num(out, "btc_move_1m") / 50.0
    move_2m = _num(out, "btc_move_2m") / 50.0
    prob_move = _num(out, "mid_up_prob_change_2m") / 0.05
    direction_score = 0.4 * move_1m + 0.4 * move_2m + 0.2 * prob_move
    uncertainty = (1.0 - 2.0 * (_num(out, "mid_up_prob_2m").fillna(0.5) - 0.5).abs()).clip(0.0, 1.0)
    same_horizon_sign = (np.sign(move_1m) == np.sign(move_2m)) & (np.sign(move_1m) != 0)
    same_proxy_sign = np.sign(direction_score) == np.sign(breadth_proxy)
    valid = same_horizon_sign & same_proxy_sign & (breadth_proxy.abs() >= 0.05)
    factor = (np.sign(direction_score) * breadth_proxy.abs() * (0.5 + 0.5 * uncertainty)).where(valid)
    out[STRATEGY] = factor
    out[f"{STRATEGY}_consensus"] = valid
    out[f"{STRATEGY}_side"] = "skip"
    out[f"{STRATEGY}_minute"] = -1
    out[f"{STRATEGY}_fraction"] = 0.04
    out[f"{STRATEGY}_component"] = "breadth_proxy_multi_window_consensus_with_reversal_veto"
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


def _blocks(names: list[str]) -> list[list[str]]:
    if len(names) != sum(BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_HOLDOUT59")
    result, cursor = [], 0
    for size in BLOCK_SIZES:
        result.append(names[cursor : cursor + size])
        cursor += size
    return result


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    features, _coverage, runs = baseline.study.prepare_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    features = add_candidate(features)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    selection = features[features["run_name"].isin(selection_runs)]
    values = pd.to_numeric(selection.loc[selection[f"{STRATEGY}_consensus"], STRATEGY], errors="coerce").dropna()
    if len(values) < 20:
        raise RuntimeError("INSUFFICIENT_SELECTION_CONSENSUS_ROWS")
    low = float(values.quantile(0.20))
    high = float(values.quantile(0.80))
    if not np.isfinite(low) or not np.isfinite(high) or low >= high:
        raise RuntimeError("INVALID_FIXED_CONSENSUS_THRESHOLDS")
    features = apply_thresholds(features, low, high)

    rows: list[dict[str, object]] = []
    for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs)):
        for slippage in SLIPPAGES:
            _, metrics = baseline.simulate_cash_locked(
                features[features["run_name"].isin(names)], STRATEGY,
                baseline.study.ExecutionConfig(fixed_slippage=slippage),
                10.0, 5.0, 600.0, cooldown_events=0, capture_logs=False,
            )
            rows.append({"variant": STRATEGY, "split": split, "slippage": slippage, **metrics})
    for index, names in enumerate(_blocks(holdout_runs), start=1):
        for slippage in SLIPPAGES:
            _, metrics = baseline.simulate_cash_locked(
                features[features["run_name"].isin(names)], STRATEGY,
                baseline.study.ExecutionConfig(fixed_slippage=slippage),
                10.0, 5.0, 600.0, cooldown_events=0, capture_logs=False,
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
    meta = {
        "format": "old-data-causal-quantml-breadth-consensus-v1",
        "hypothesis_id": "QML-P4-breadth-consensus-reversal-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_sizes": list(BLOCK_SIZES),
        "factor": "sign(0.4*btc_move_1m+0.4*btc_move_2m+0.2*pm_prob_move_2m)*abs(mean(buy_size_imbalance_2m,sell_size_imbalance_2m))*(0.5+0.5*uncertainty)",
        "fixed_veto": "require sign(btc_move_1m)==sign(btc_move_2m)==sign(direction_score)==sign(breadth_proxy) and abs(breadth_proxy)>=0.05",
        "thresholds": {"selection_quantiles": [0.20, 0.80], "low": low, "high": high},
        "protocol": {"initial_cash": 10.0, "minimum_order_notional": 5.0, "fee_rate": 0.07, "settlement_seconds": 300.0, "cash_release_delay_seconds": 600.0, "slippages": list(SLIPPAGES), "entry_minute": 2, "cooldown_events": 0},
        "summary": {"holdout_trades": int(holdout["trades"]), "holdout_ending_cash_005": float(holdout["ending_cash"]), "minimum_block_ending_cash": float(blocks["ending_cash"].min()), "minimum_block_net_return": float(blocks["net_return"].min()), "gate_passed": gate},
        "decision": "promote_only_if_dual_slippage_holdout_blocks_pass",
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "paper_only": True,
        "evidence_boundary": "Historical old-data paper replay; no order acceptance, settlement, reusable cash receipt or canary fill.",
        "source_basis": "Agent-reviewed QuantML ETF breadth/threshold report; analogy only, not direct BTC5m evidence."
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
