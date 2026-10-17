"""Stress the frozen path-efficiency paper candidate under longer cash locks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from v1_state_filter_walk_forward import filtered_frame

STRATEGY = "v1_adaptive_mix"
FILTER = "path_efficiency_ge_050"
DELAYS = (600.0, 1200.0, 1800.0)
SLIPPAGES = (0.005, 0.01, 0.015)
BLOCK_SIZES = (16, 16, 16, 16, 15)


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    gated = filtered_frame(features, STRATEGY, FILTER)
    test_runs = runs[4:]
    holdout_runs = test_runs[20:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(test_runs[cursor : cursor + size])
        cursor += size
    rows: list[dict[str, object]] = []
    logs: list[pd.DataFrame] = []
    for delay in DELAYS:
        for slippage in SLIPPAGES:
            config = study.ExecutionConfig(fixed_slippage=slippage)
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(holdout_runs)], STRATEGY, config,
                10.0, 5.0, delay, cooldown_events=0, capture_logs=True,
            )
            rows.append({"split": "holdout59", "delay_seconds": delay, "slippage": slippage, **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["split"] = "holdout59"; trade_log["delay_seconds"] = delay; trade_log["slippage"] = slippage; logs.append(trade_log)
            for block_index, names in enumerate(blocks, 1):
                trade_log, metrics = simulate_cash_locked(
                    gated[gated["run_name"].isin(names)], STRATEGY, config,
                    10.0, 5.0, delay, cooldown_events=0, capture_logs=True,
                )
                rows.append({"split": f"block{block_index}", "delay_seconds": delay, "slippage": slippage, **metrics})
                if not trade_log.empty:
                    trade_log = trade_log.copy(); trade_log["split"] = f"block{block_index}"; trade_log["delay_seconds"] = delay; trade_log["slippage"] = slippage; logs.append(trade_log)
    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    if logs:
        pd.concat(logs, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    gate_rows = result[(result["split"].str.startswith("block")) & (result["ending_cash"] >= 5.0) & (result["net_return"] >= 0.0)]
    stress_gate = bool(len(gate_rows) == len(BLOCK_SIZES) * len(DELAYS) * len(SLIPPAGES))
    meta = {
        "format": "old-data-causal-path-efficiency-cash-stress-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "feature_rows": len(features),
        "strategy": STRATEGY, "filter": "path_efficiency_first2m >= 0.50", "holdout_runs": len(holdout_runs),
        "delays_seconds": list(DELAYS), "slippages": list(SLIPPAGES),
        "protocol": {"initial_cash": 10.0, "min_cost": 5.0, "fee_rate": 0.07, "settlement_seconds": 300.0, "fill_ratio": 0.25, "cooldown_events": 0},
        "stress_gate": "all five blocks must end >=$5 with nonnegative net return for every delay and slippage",
        "stress_gate_passed": stress_gate, "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(result.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()
