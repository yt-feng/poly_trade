"""Cost-aware candidate on the frozen clean old-data snapshot.

The candidate does not tune against test outcomes.  It uses the existing
causal rolling-logit estimate and a fixed execution-cost floor:

    predicted probability edge >= taker fee + half spread + one tick + buffer

Three buffers are frozen before evaluation.  Selection uses only an expanding
train prefix and a four-run validation window; the following nine-run block is
untouched until the candidate is fixed.  This remains historical paper replay
only.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from cash_release_sensitivity import simulate_cash_locked
import v1_execution_walk_forward as study
from nested_walk_forward_registry import (
    INITIAL_TRAIN_RUNS,
    MINIMUM_ORDER,
    MIN_TRAIN_TRADES,
    MIN_VALIDATION_TRADES,
    SOURCE_OFFSET,
    TEST_BLOCK_SIZES,
    VALIDATION_RUNS,
    outer_folds,
)
from registry_small_cash_holdout import (
    DELAY_SECONDS,
    EXPECTED_COMPLETE_RUNS,
    INITIAL_CASH,
    SLIPPAGE,
    TAKER_FEE_RATE,
    prepare_registry_data,
)


CANDIDATES = (
    ("fee_spread_floor", 0.00),
    ("fee_spread_plus_1tick", 0.01),
    ("fee_spread_plus_2tick", 0.02),
)
MIN_SPREAD = 0.06
MAX_OVERRUN = 0.04
MIN_DEPTH = 120.0
TICK = 0.01


def candidate_name(buffer: float) -> str:
    for name, value in CANDIDATES:
        if math.isclose(value, buffer, abs_tol=1e-12):
            return name
    raise ValueError("UNREGISTERED_COST_BUFFER")


def add_signals(features: pd.DataFrame, name: str) -> pd.DataFrame:
    spec = dict(CANDIDATES).get(name)
    if spec is None:
        raise ValueError("UNKNOWN_EXECUTION_AWARE_CANDIDATE")
    out = features.copy()
    q = pd.to_numeric(out["pred_prob_up_logit"], errors="coerce")
    choices = []
    for _, row in out.iterrows():
        prob = q.loc[row.name]
        best = None
        if pd.notna(prob):
            for side, probability, price_col, spread_col, size_col in (
                ("buy_up", float(prob), "buy_up_price_2m", "spread_up_median_first2m", "buy_up_size_2m"),
                ("buy_down", 1.0 - float(prob), "buy_down_price_2m", "spread_down_median_first2m", "buy_down_size_2m"),
            ):
                price = pd.to_numeric(row.get(price_col), errors="coerce")
                spread = pd.to_numeric(row.get(spread_col), errors="coerce")
                size = pd.to_numeric(row.get(size_col), errors="coerce")
                overround = pd.to_numeric(row.get("overround_median_first2m"), errors="coerce")
                if not all(pd.notna(x) for x in (price, spread, size, overround)):
                    continue
                if not (0.05 <= float(price) <= 0.95 and float(spread) <= MIN_SPREAD
                        and float(overround) <= MAX_OVERRUN and float(size) >= MIN_DEPTH):
                    continue
                fee = TAKER_FEE_RATE * float(price) * (1.0 - float(price))
                net_edge = float(probability) - float(price) - fee - 0.5 * float(spread) - TICK
                if net_edge < float(spec):
                    continue
                if best is None or net_edge > best[0]:
                    best = (net_edge, side)
        if best is None:
            choices.append(("skip", -1, 0.0, "none"))
        else:
            choices.append((best[1], 2, 0.05, f"{name}_net_edge"))
    choice = pd.DataFrame(choices, index=out.index, columns=["side", "minute", "fraction", "component"])
    out[f"{name}_side"] = choice["side"]
    out[f"{name}_minute"] = choice["minute"].astype(int)
    out[f"{name}_fraction"] = choice["fraction"].astype(float)
    out[f"{name}_component"] = choice["component"]
    return out


def metric(frame: pd.DataFrame, name: str, runs: list[str]) -> dict[str, object]:
    _logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(runs)],
        name,
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


def eligible_train(row: dict[str, object]) -> bool:
    return int(row["trades"]) >= MIN_TRAIN_TRADES and float(row["ending_cash"]) >= MINIMUM_ORDER and float(row["net_return"]) >= 0.0


def eligible_validation(row: dict[str, object]) -> bool:
    return int(row["trades"]) >= MIN_VALIDATION_TRADES and float(row["ending_cash"]) >= MINIMUM_ORDER and float(row["net_return"]) >= 0.0


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = prepare_registry_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    frames = {name: add_signals(features, name) for name, _ in CANDIDATES}
    train_validation: list[dict[str, object]] = []
    test_rows: list[dict[str, object]] = []
    selected_by_fold: list[dict[str, object]] = []
    for fold in outer_folds(runs):
        index = int(fold["fold"])
        train_names = list(fold["train_runs"])
        validation_names = list(fold["validation_runs"])
        test_names = list(fold["test_runs"])
        candidates: list[dict[str, object]] = []
        for name, buffer in CANDIDATES:
            train = {"candidate": name, "buffer": buffer, "fold": index, "split": "train", **metric(frames[name], name, train_names)}
            validation = {"candidate": name, "buffer": buffer, "fold": index, "split": "validation", **metric(frames[name], name, validation_names)}
            train_validation.extend([train, validation])
            if eligible_train(train) and eligible_validation(validation):
                candidates.append(validation)
        candidates.sort(key=lambda row: (float(row["ending_cash"]), int(row["trades"]), -float(row["max_cash_drawdown"]), str(row["candidate"])), reverse=True)
        winner = None if not candidates else candidates[0]
        test = None
        if winner is not None:
            name = str(winner["candidate"])
            test = {"candidate": name, "buffer": winner["buffer"], "fold": index, "split": "test", **metric(frames[name], name, test_names)}
            test_rows.append(test)
        selected_by_fold.append({"fold": index, "train_runs": len(train_names), "validation_runs": len(validation_names), "test_runs": len(test_names), "selected_candidate": None if winner is None else winner["candidate"], "eligible_validation_candidates": len(candidates), "validation_winner": winner, "test_result": test})

    test_cash = [float(row["ending_cash"]) for row in test_rows]
    test_net = [float(row["net_return"]) for row in test_rows]
    test_trades = sum(int(row["trades"]) for row in test_rows)
    gate_passed = bool(len(test_rows) == len(TEST_BLOCK_SIZES) and test_trades >= 10 and all(x >= MINIMUM_ORDER for x in test_cash) and all(x >= 0 for x in test_net))
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(train_validation).to_csv(output / "train_validation_metrics.csv", index=False)
    pd.DataFrame(test_rows).to_csv(output / "test_metrics.csv", index=False)
    meta = {
        "format": "old-data-execution-aware-cost-floor-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "source_offset_runs": SOURCE_OFFSET,
        "initial_train_runs": INITIAL_TRAIN_RUNS,
        "validation_runs_per_fold": VALIDATION_RUNS,
        "test_block_sizes": list(TEST_BLOCK_SIZES),
        "candidates": [{"name": name, "buffer": value} for name, value in CANDIDATES],
        "execution_filter": {"fee_rate": TAKER_FEE_RATE, "half_spread": True, "tick": TICK, "max_spread": MIN_SPREAD, "max_overround": MAX_OVERRUN, "min_depth": MIN_DEPTH},
        "selection_rule": "expanding train >=3 trades/nonnegative and validation >=1 trade/nonnegative; rank validation ending cash, then trades, drawdown, name; evaluate next unseen test block",
        "selected_by_fold": selected_by_fold,
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "taker_fee_rate": TAKER_FEE_RATE, "settlement_seconds": 300.0, "cash_release_delay_seconds": DELAY_SECONDS, "slippage": SLIPPAGE, "cooldown_events": 0},
        "summary": {"test_blocks": len(test_rows), "test_trades": test_trades, "test_block_ending_cash": test_cash, "test_block_net_return": test_net, "minimum_test_block_ending_cash": None if not test_cash else min(test_cash), "minimum_test_block_net_return": None if not test_net else min(test_net), "gate_passed": gate_passed},
        "research_sources": [
            "https://github.com/Fierdimo/polymarket-analysis",
            "https://github.com/gregyoung14/openmarket",
            "https://arxiv.org/abs/2602.00133",
        ],
        "decision": "promote_only_if_five_unseen_cost_aware_test_blocks_pass",
        "candidate_promoted_to_canary": False,
        "evidence_boundary": "Historical old-data paper replay only; no order acceptance, settlement, reusable-cash receipt, or Canary fill.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True, allow_nan=False))
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
