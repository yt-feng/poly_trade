"""Causal entry-price state filter under the frozen small-cash contract.

The existing v1 adaptive/conservative signal family is replayed with the
registered causal entry-price state filter ``0.20 <= entry price <= 0.80``.
Both base components are evaluated under the documented $5 notional, crypto
taker fee, settlement and cash-release rules. Selection is limited to the
first 20 test runs; the remaining 59 runs and five chronological blocks are
held out. No order is submitted and no simulated fill is a canary.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from v1_state_filter_walk_forward import filtered_frame

CANDIDATES = (
    "v1_adaptive_mix__entry_price_midrange_020_080",
    "v1_conservative_mix__entry_price_midrange_020_080",
)
BLOCK_SIZES = (16, 16, 16, 16, 15)


def evaluate(source_root: Path, output: Path, slippages: tuple[float, ...]) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(test_runs[cursor:cursor + size])
        cursor += size

    rows: list[dict[str, object]] = []
    logs: list[pd.DataFrame] = []
    for candidate in CANDIDATES:
        strategy, filter_name = candidate.split("__", 1)
        gated = filtered_frame(features, strategy, filter_name)
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            config = study.ExecutionConfig(fixed_slippage=slippages[0])
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                600.0, cooldown_events=0, capture_logs=True,
            )
            rows.append({"candidate": candidate, "split": split, "slippage": slippages[0], **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = split; logs.append(trade_log)

        for slippage in slippages:
            config = study.ExecutionConfig(fixed_slippage=slippage)
            for block_index, names in enumerate(blocks, 1):
                trade_log, metrics = simulate_cash_locked(
                    gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                    600.0, cooldown_events=0, capture_logs=True,
                )
                rows.append({"candidate": candidate, "split": f"block{block_index}", "slippage": slippage, **metrics})
                if not trade_log.empty:
                    trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = f"block{block_index}"; logs.append(trade_log)
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(test_runs)], strategy, config, 10.0, 5.0,
                600.0, cooldown_events=0, capture_logs=True,
            )
            rows.append({"candidate": candidate, "split": "all_test79", "slippage": slippage, **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = "all_test79"; logs.append(trade_log)

    result = pd.DataFrame(rows)
    selection = result[(result["split"] == "selection20") & result["slippage"].eq(slippages[0])]
    selected_candidate = str(selection.sort_values(["ending_cash", "trades"], ascending=[False, False]).iloc[0]["candidate"])
    selected_rows = result[result["candidate"].eq(selected_candidate)].copy()
    holdout = selected_rows[selected_rows["split"].eq("holdout59")]
    blocks_selected = selected_rows[selected_rows["split"].str.startswith("block")]
    gate = bool(
        int(holdout["trades"].iloc[0]) >= 10
        and len(blocks_selected) == len(BLOCK_SIZES) * len(slippages)
        and bool((blocks_selected["ending_cash"] >= 5.0).all())
        and bool((blocks_selected["net_return"] >= 0.0).all())
    )

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    if logs:
        pd.concat(logs, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    selected_rows.to_csv(output / "selected_metrics.csv", index=False)
    meta = {
        "format": "old-data-causal-entry-price-cash-replay-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "feature_rows": len(features),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "candidates": list(CANDIDATES), "selected_candidate": selected_candidate,
        "filter": "0.20 <= entry_price <= 0.80",
        "protocol": {"initial_cash": 10.0, "min_cost": 5.0, "fee_rate": 0.07,
                     "settlement_seconds": 300.0, "cash_release_delay_seconds": 600.0,
                     "slippages": list(slippages), "fill_ratio": 0.25, "cooldown_events": 0},
        "promotion_rule": "selected holdout must have >=10 trades and all five blocks must end >=$5 with nonnegative PnL at both slippages",
        "selected_gate_passed": gate, "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(selected_rows.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--slippages", default="0.005,0.010")
    args = parser.parse_args()
    slippages = tuple(float(value.strip()) for value in args.slippages.split(",") if value.strip())
    if slippages != (0.005, 0.01):
        raise SystemExit("This preregistered replay requires 0.005 and 0.01 slippage")
    evaluate(args.source_root, args.output, slippages)


if __name__ == "__main__":
    main()
