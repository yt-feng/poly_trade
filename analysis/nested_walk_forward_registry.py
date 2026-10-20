"""Nested walk-forward selection on the frozen clean old-data snapshot.

Each outer test block is evaluated only after an expanding training prefix and
a separate validation window.  A strategy selected in one fold is never
selected using that fold's test block.  This is historical paper evidence only
and keeps the small-cash execution contract identical to the registry audits.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from cash_release_sensitivity import simulate_cash_locked
import v1_execution_walk_forward as study
from registry_small_cash_holdout import (
    EXPECTED_COMPLETE_RUNS,
    INITIAL_CASH,
    MINIMUM_ORDER,
    REGISTRY_NAMES,
    SLIPPAGE,
    TAKER_FEE_RATE,
    DELAY_SECONDS,
    add_signals,
    prepare_registry_data,
)


SOURCE_OFFSET = 4
INITIAL_TRAIN_RUNS = 14
VALIDATION_RUNS = 4
TEST_BLOCK_SIZES = (9, 9, 9, 9, 9)
MIN_TRAIN_TRADES = 3
MIN_VALIDATION_TRADES = 1


def outer_folds(runs: list[str]) -> list[dict[str, list[str] | int]]:
    """Build expanding train/validation/test folds without overlap in tests."""
    if len(runs) < SOURCE_OFFSET + INITIAL_TRAIN_RUNS + sum(
        VALIDATION_RUNS + size for size in TEST_BLOCK_SIZES
    ):
        raise ValueError("INSUFFICIENT_RUNS_FOR_NESTED_WALK_FORWARD")
    test_runs = runs[SOURCE_OFFSET:]
    train_end = INITIAL_TRAIN_RUNS
    folds: list[dict[str, list[str] | int]] = []
    for index, test_size in enumerate(TEST_BLOCK_SIZES, 1):
        validation_start = train_end
        validation_end = validation_start + VALIDATION_RUNS
        test_start = validation_end
        test_end = test_start + test_size
        folds.append(
            {
                "fold": index,
                "train_runs": test_runs[:train_end],
                "validation_runs": test_runs[validation_start:validation_end],
                "test_runs": test_runs[test_start:test_end],
            }
        )
        train_end = test_end
    return folds


def _metric(frame: pd.DataFrame, strategy: str, names: list[str]) -> dict[str, object]:
    _logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(names)],
        strategy,
        study.ExecutionConfig(fixed_slippage=SLIPPAGE),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=False,
        fee_mode="polymarket_taker",
        fee_rate=TAKER_FEE_RATE,
    )
    return metrics


def _eligible_train(row: dict[str, object]) -> bool:
    return (
        int(row["trades"]) >= MIN_TRAIN_TRADES
        and float(row["ending_cash"]) >= MINIMUM_ORDER
        and float(row["net_return"]) >= 0.0
    )


def _eligible_validation(row: dict[str, object]) -> bool:
    return (
        int(row["trades"]) >= MIN_VALIDATION_TRADES
        and float(row["ending_cash"]) >= MINIMUM_ORDER
        and float(row["net_return"]) >= 0.0
    )


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = prepare_registry_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    folds = outer_folds(runs)

    # Signal construction is deterministic for a frozen feature table.  Build
    # each registry frame once so nested folds spend their time on the
    # chronological cash replay rather than repeating identical transforms.
    strategy_frames = {
        strategy: add_signals(features, strategy) for strategy in REGISTRY_NAMES
    }

    train_validation_rows: list[dict[str, object]] = []
    test_rows: list[dict[str, object]] = []
    selected_rows: list[dict[str, object]] = []
    selected_by_fold: list[dict[str, object]] = []

    for fold in folds:
        fold_index = int(fold["fold"])
        train_names = list(fold["train_runs"])
        validation_names = list(fold["validation_runs"])
        test_names = list(fold["test_runs"])
        candidates: list[dict[str, object]] = []
        for strategy in REGISTRY_NAMES:
            frame = strategy_frames[strategy]
            train = {"strategy": strategy, "fold": fold_index, "split": "train", **_metric(frame, strategy, train_names)}
            validation = {"strategy": strategy, "fold": fold_index, "split": "validation", **_metric(frame, strategy, validation_names)}
            train_validation_rows.extend([train, validation])
            if _eligible_train(train) and _eligible_validation(validation):
                candidates.append(validation)

        candidates.sort(
            key=lambda row: (
                float(row["ending_cash"]),
                int(row["trades"]),
                -float(row["max_cash_drawdown"]),
                str(row["strategy"]),
            ),
            reverse=True,
        )
        selected_strategy = None if not candidates else str(candidates[0]["strategy"])
        selected_validation = None if not candidates else candidates[0]
        selected_test = None
        if selected_strategy is not None:
            frame = strategy_frames[selected_strategy]
            selected_test = {"strategy": selected_strategy, "fold": fold_index, "split": "test", **_metric(frame, selected_strategy, test_names)}
            test_rows.append(selected_test)
            selected_rows.append({**selected_test, "validation_ending_cash": selected_validation["ending_cash"], "validation_trades": selected_validation["trades"]})
        selected_by_fold.append(
            {
                "fold": fold_index,
                "train_runs": len(train_names),
                "validation_runs": len(validation_names),
                "test_runs": len(test_names),
                "selected_strategy": selected_strategy,
                "eligible_validation_candidates": len(candidates),
                "validation_winner": selected_validation,
                "test_result": selected_test,
            }
        )

    test_frame = pd.DataFrame(test_rows)
    selected_test_count = int(test_frame["trades"].sum()) if not test_frame.empty else 0
    test_block_cash = [float(row["ending_cash"]) for row in test_rows]
    test_block_net = [float(row["net_return"]) for row in test_rows]
    gate_passed = bool(
        len(test_rows) == len(TEST_BLOCK_SIZES)
        and selected_test_count >= 10
        and all(value >= MINIMUM_ORDER for value in test_block_cash)
        and all(value >= 0.0 for value in test_block_net)
    )

    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(train_validation_rows).to_csv(output / "train_validation_metrics.csv", index=False)
    pd.DataFrame(selected_rows).to_csv(output / "nested_test_metrics.csv", index=False)
    meta: dict[str, object] = {
        "format": "old-data-nested-walk-forward-registry-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "registry_names": list(REGISTRY_NAMES),
        "source_offset_runs": SOURCE_OFFSET,
        "initial_train_runs": INITIAL_TRAIN_RUNS,
        "validation_runs_per_fold": VALIDATION_RUNS,
        "test_block_sizes": list(TEST_BLOCK_SIZES),
        "minimum_train_trades": MIN_TRAIN_TRADES,
        "minimum_validation_trades": MIN_VALIDATION_TRADES,
        "selection_rule": "within each expanding fold, require train >=3 trades/nonnegative and validation >=1 trade/nonnegative; rank eligible validation candidates by ending cash, trades, drawdown, name; evaluate only the next unseen test block",
        "selected_by_fold": selected_by_fold,
        "protocol": {
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "taker_fee_rate": TAKER_FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippage": SLIPPAGE,
            "cooldown_events": 0,
        },
        "summary": {
            "test_blocks": len(test_rows),
            "test_trades": selected_test_count,
            # Each outer test block is an independent historical evaluation
            # that starts from INITIAL_CASH.  Summing trades across blocks is
            # useful for transfer diagnostics, but it is never one continuous
            # compounding ledger and must not satisfy the ten-Canary gate.
            "same_ledger_compounding_rows": 0,
            "same_ledger_gate_passed": False,
            "cross_block_trade_sum_is_not_same_ledger": True,
            "test_block_ending_cash": test_block_cash,
            "test_block_net_return": test_block_net,
            "minimum_test_block_ending_cash": None if not test_block_cash else min(test_block_cash),
            "minimum_test_block_net_return": None if not test_block_net else min(test_block_net),
            "gate_passed": gate_passed,
        },
        "decision": "promote_only_if_nested_out_of_sample_gate_passes",
        "candidate_promoted_to_canary": False,
        "evidence_boundary": "Historical old-data paper replay only; no order acceptance, settlement, reusable-cash receipt, or Canary fill.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
