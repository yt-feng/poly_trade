"""H59: test the existing strategy registry under the real ten-fill ledger gate.

H57 and H58 showed that the two frozen H30/H38 signals cannot be repaired by
entry-time gates or a more stable gate selector.  H59 reuses the existing
pre-registered strategy registry, but evaluates it with the same H55 execution
contract: fee, tick rounding, observed depth, slippage, minimum shares and
cash-release delay.  Every strategy must first pass two contiguous ten-run
selection blocks under nominal and stress before one winner is evaluated on
the nine rolling holdout windows.

This is a paper replay only.  Each block is evaluated with its own starting
cash for selection/holdout comparison; the ten fills within a block are
chronological on one evolving ledger.  Blocks are never concatenated into a
synthetic Canary chain.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
# Keep the registry feature construction and signal definitions in the
# existing module; no strategy is invented or refit in this audit.
import h55_tick_depth_prefix_audit as h55
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS
from registry_small_cash_holdout import REGISTRY_NAMES, add_signals, prepare_registry_data

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
SELECTION_BLOCK_SIZE = 10
SELECTION_BLOCK_COUNT = 2
INITIAL_CASH = h55.INITIAL_CASH
MINIMUM_ORDER_NOTIONAL = h55.MINIMUM_ORDER_NOTIONAL
MINIMUM_ORDER_SHARES = h55.MINIMUM_ORDER_SHARES
FEE_RATE = h55.FEE_RATE
CASH_RELEASE_DELAY = h55.CASH_RELEASE_DELAY_SECONDS


def contiguous_selection_blocks(names: list[str]) -> list[tuple[int, list[str]]]:
    expected = SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT
    if len(names) != expected:
        raise ValueError(f"SELECTION_RUN_COUNT_MISMATCH:{len(names)}:{expected}")
    return [
        (index + 1, names[index * SELECTION_BLOCK_SIZE : (index + 1) * SELECTION_BLOCK_SIZE])
        for index in range(SELECTION_BLOCK_COUNT)
    ]


def replay_registry(
    frame: pd.DataFrame,
    strategy: str,
    names: list[str],
    tick_size: float,
    depth_floor: float,
    slippage: float,
) -> dict[str, object]:
    rounded = h55.round_quote_prices(frame, tick_size)
    logs, metrics = simulate_cash_locked(
        rounded[rounded["run_name"].astype(str).isin([str(name) for name in names])],
        strategy,
        study.ExecutionConfig(fixed_slippage=float(slippage), min_depth=float(depth_floor)),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        CASH_RELEASE_DELAY,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {**metrics, **h55.chain_summary(h55.paper_events(logs))}


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, _coverage, runs = prepare_registry_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    test_runs = runs[4:]
    selection_runs = test_runs[: SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT]
    holdout_runs = test_runs[SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT :]
    selection_blocks = contiguous_selection_blocks(selection_runs)
    holdout_windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, object]] = []
    strategy_frames: dict[str, pd.DataFrame] = {}
    for strategy in REGISTRY_NAMES:
        strategy_frames[strategy] = add_signals(features, strategy)
        for block_index, names in selection_blocks:
            for config in CONFIGS:
                metrics = replay_registry(
                    strategy_frames[strategy],
                    strategy,
                    names,
                    config["tick_size"],
                    config["depth_floor"],
                    config["slippage"],
                )
                selection_rows.append(
                    {
                        "strategy": strategy,
                        "selection_block": block_index,
                        "split": f"selection_block_{block_index}",
                        **config,
                        **metrics,
                    }
                )

    selection = pd.DataFrame(selection_rows)
    stable_candidates: list[dict[str, Any]] = []
    for strategy in REGISTRY_NAMES:
        rows = selection[selection["strategy"].eq(strategy)]
        if len(rows) != len(selection_blocks) * len(CONFIGS):
            continue
        if bool(rows["first_ten_passed"].all()) and bool((rows["first_ten_fills"] == 10).all()):
            stable_candidates.append(
                {
                    "strategy": strategy,
                    "min_selection_cash": float(rows["first_ten_ending_cash_usd"].min()),
                    "selection_cash_sum": float(rows["first_ten_ending_cash_usd"].sum()),
                    "min_selection_margin": float(
                        (rows["first_ten_ending_cash_usd"] - INITIAL_CASH).min()
                    ),
                }
            )

    stable_candidates = sorted(
        stable_candidates,
        key=lambda row: (-row["min_selection_cash"], -row["selection_cash_sum"], row["strategy"]),
    )
    selected_strategy = stable_candidates[0]["strategy"] if stable_candidates else None

    holdout_rows: list[dict[str, object]] = []
    if selected_strategy is not None:
        # The winner is fixed from selection only.  Holdout never participates
        # in choosing the strategy or execution configuration.
        for window_index, (_start, names) in enumerate(holdout_windows, start=1):
            for config in CONFIGS:
                metrics = replay_registry(
                    strategy_frames[selected_strategy],
                    selected_strategy,
                    names,
                    config["tick_size"],
                    config["depth_floor"],
                    config["slippage"],
                )
                holdout_rows.append(
                    {
                        "strategy": selected_strategy,
                        "split": f"holdout_window_{window_index}",
                        "window_index": window_index,
                        **config,
                        **metrics,
                    }
                )

    holdout = pd.DataFrame(holdout_rows)
    holdout_gate = bool(
        selected_strategy is not None
        and len(holdout) == len(holdout_windows) * len(CONFIGS)
        and holdout["first_ten_passed"].all()
        and (holdout["first_ten_fills"] == 10).all()
    )
    summary = {
        "format": "old-clean-h59-registry-same-ledger-rolling-audit-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "registry_names": list(REGISTRY_NAMES),
        "selection_blocks": [
            {"block_index": index, "run_count": len(names), "first_run": names[0], "last_run": names[-1]}
            for index, names in selection_blocks
        ],
        "selection_rule": (
            "require every registry strategy to pass exact ten chronological fills with positive "
            "first-ten cash growth in both contiguous selection blocks and nominal/stress configs; "
            "rank eligible strategies by worst-block ending cash, then summed ending cash, then name; "
            "evaluate only the selected strategy on untouched rolling holdout windows"
        ),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY,
            "configs": list(CONFIGS),
            "same_ledger": True,
            "required_fills": 10,
            "no_concatenation": True,
            "no_refit": True,
            "selection_block_size_runs": SELECTION_BLOCK_SIZE,
            "selection_block_count": SELECTION_BLOCK_COUNT,
        },
        "window_size_runs": WINDOW_SIZE,
        "window_stride_runs": WINDOW_STRIDE,
        "window_count": len(holdout_windows),
        "stable_selection_candidates": stable_candidates,
        "selected_strategy": selected_strategy,
        "holdout_gate": holdout_gate,
        "selection_rows": selection.to_dict(orient="records"),
        "holdout_rows": holdout.to_dict(orient="records"),
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
                "stable_candidates": [row["strategy"] for row in stable_candidates],
                "selected_strategy": selected_strategy,
                "holdout_gate": holdout_gate,
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
