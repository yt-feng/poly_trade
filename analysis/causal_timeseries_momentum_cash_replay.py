"""Causal time-series momentum under the frozen small-cash contract.

The predeclared candidate family uses BTC two-minute direction, path
-efficiency, Polymarket probability change, and the registered liquidity gate.
It is evaluated on old clean data only with the same minimum-notional, fee,
settlement, cash-release and chronological-block rules used by the other
paper replays. No order is submitted and no simulated fill is a canary.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import monthly_runs_quant_research_framework as research
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked

CANDIDATES = ("timeseries_momentum_up", "timeseries_momentum_down")
BLOCK_SIZES = (16, 16, 16, 16, 15)
SLIPPAGES = (0.005, 0.01)


def attach_candidate(features: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = features.copy()
    choices = out.apply(lambda row: research.choose_trade(row, strategy), axis=1)
    out[f"{strategy}_side"] = choices.map(lambda value: value[0])
    out[f"{strategy}_minute"] = choices.map(lambda value: value[1]).astype(int)
    out[f"{strategy}_fraction"] = choices.map(lambda value: value[2]).fillna(0.0).astype(float)
    out[f"{strategy}_component"] = choices.map(lambda value: value[3])
    return out


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    features = research.add_research_features(features)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(test_runs[cursor : cursor + size])
        cursor += size

    rows: list[dict[str, object]] = []
    logs: list[pd.DataFrame] = []
    for strategy in CANDIDATES:
        gated = attach_candidate(features, strategy)
        config = study.ExecutionConfig(fixed_slippage=SLIPPAGES[0])
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                600.0, cooldown_events=0, capture_logs=True,
            )
            rows.append({"candidate": strategy, "split": split, "slippage": SLIPPAGES[0], **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["candidate"] = strategy; trade_log["split"] = split; logs.append(trade_log)
        for slippage in SLIPPAGES:
            config = study.ExecutionConfig(fixed_slippage=slippage)
            for block_index, names in enumerate(blocks, 1):
                trade_log, metrics = simulate_cash_locked(
                    gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                    600.0, cooldown_events=0, capture_logs=True,
                )
                rows.append({"candidate": strategy, "split": f"block{block_index}", "slippage": slippage, **metrics})
                if not trade_log.empty:
                    trade_log = trade_log.copy(); trade_log["candidate"] = strategy; trade_log["split"] = f"block{block_index}"; logs.append(trade_log)
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(test_runs)], strategy, config, 10.0, 5.0,
                600.0, cooldown_events=0, capture_logs=True,
            )
            rows.append({"candidate": strategy, "split": "all_test79", "slippage": slippage, **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["candidate"] = strategy; trade_log["split"] = "all_test79"; logs.append(trade_log)

    result = pd.DataFrame(rows)
    selection = result[(result["split"] == "selection20") & result["slippage"].eq(SLIPPAGES[0])]
    selected_candidate = str(selection.sort_values(["ending_cash", "trades"], ascending=[False, False]).iloc[0]["candidate"])
    selected = result[result["candidate"].eq(selected_candidate)].copy()
    holdout = selected[selected["split"].eq("holdout59")]
    blocks_selected = selected[selected["split"].str.startswith("block")]
    gate = bool(
        int(holdout["trades"].iloc[0]) >= 10
        and len(blocks_selected) == len(BLOCK_SIZES) * len(SLIPPAGES)
        and bool((blocks_selected["ending_cash"] >= 5.0).all())
        and bool((blocks_selected["net_return"] >= 0.0).all())
    )
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    selected.to_csv(output / "selected_metrics.csv", index=False)
    if logs:
        pd.concat(logs, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    meta = {
        "format": "old-data-causal-timeseries-momentum-cash-replay-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "feature_rows": len(features),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "candidates": list(CANDIDATES), "selected_candidate": selected_candidate,
        "hypothesis": "btc_move_2m direction + path_efficiency_first2m + mid_up_prob_change_2m + quality gate",
        "protocol": {"initial_cash": 10.0, "min_cost": 5.0, "fee_rate": 0.07,
                     "settlement_seconds": 300.0, "cash_release_delay_seconds": 600.0,
                     "slippages": list(SLIPPAGES), "fill_ratio": 0.25, "cooldown_events": 0},
        "promotion_rule": "selected holdout must have >=10 trades and all five blocks must end >=$5 with nonnegative PnL at both slippages",
        "selected_gate_passed": gate, "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(selected.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()
