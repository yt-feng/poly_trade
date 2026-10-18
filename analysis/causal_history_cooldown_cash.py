"""Strict holdout-only audit of the registered history-gated early-drop candidate.

The candidate and cooldown were selected before this audit: the base
``classic_early_drop_down`` signal is allowed only after 20 prior executable
history observations have positive mean net return, and a 12-event cooldown is
used after two consecutive losses.  This replay only changes the audit
partition: five chronological blocks cover holdout59, with cash locked until
five-minute settlement plus 600 seconds.
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

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_robust_gate_sweep import attach_robust_gate
from classic_min_order_chain import prepare


EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUNS = 20
BLOCK_SIZES = (12, 12, 12, 12, 11)
SLIPPAGES = (0.005, 0.010, 0.015)
CANDIDATE = "classic_early_drop_down"
GATE = "mean_positive"
STRATEGY = f"{CANDIDATE}_{GATE}"
MIN_HISTORY = 20
LOOKBACK = 20
COOLDOWN_EVENTS = 12
CASH_RELEASE_DELAY_SECONDS = 600.0


def holdout_blocks(runs: list[str]) -> list[list[str]]:
    if len(runs) != sum(BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_HOLDOUT59")
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")

    base, runs = prepare(source_root, [CANDIDATE])
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    gated, diagnostic = attach_robust_gate(base, GATE, MIN_HISTORY, LOOKBACK)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    blocks = holdout_blocks(holdout_runs)

    rows: list[dict[str, object]] = []
    split_names: list[tuple[str, list[str]]] = [
        ("selection20", selection_runs),
        ("holdout59", holdout_runs),
    ]
    split_names.extend((f"block{index}", names) for index, names in enumerate(blocks, start=1))
    for split, names in split_names:
        frame = gated[gated["run_name"].isin(names)]
        for slippage in SLIPPAGES:
            _, metrics = simulate_cash_locked(
                frame,
                STRATEGY,
                study.ExecutionConfig(fixed_slippage=slippage),
                10.0,
                5.0,
                CASH_RELEASE_DELAY_SECONDS,
                cooldown_events=COOLDOWN_EVENTS,
                capture_logs=False,
            )
            rows.append({"split": split, "slippage": slippage, **metrics})

    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    holdout = result[(result["split"] == "holdout59") & (result["slippage"] == SLIPPAGES[0])].iloc[0]
    block_rows = result[result["split"].str.startswith("block")]
    gate_passed = bool(
        int(holdout["trades"]) >= 10
        and (block_rows["ending_cash"] >= 5.0).all()
        and (block_rows["net_return"] >= 0.0).all()
    )
    meta: dict[str, object] = {
        "format": "old-data-causal-history-cooldown-cash-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(gated),
        "candidate": CANDIDATE,
        "gate": GATE,
        "min_history": MIN_HISTORY,
        "lookback": LOOKBACK,
        "cooldown_events": COOLDOWN_EVENTS,
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_scope": "holdout59_only",
        "block_sizes": list(BLOCK_SIZES),
        "protocol": {
            "initial_cash": 10.0,
            "minimum_order_notional": 5.0,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "new_data_collected": False,
            "live_orders_enabled": False,
        },
        "diagnostic": diagnostic,
        "summary": {
            "holdout_trades": int(holdout["trades"]),
            "holdout_ending_cash_005": float(holdout["ending_cash"]),
            "holdout_net_return_005": float(holdout["net_return"]),
            "minimum_block_ending_cash": float(block_rows["ending_cash"].min()),
            "minimum_block_net_return": float(block_rows["net_return"].min()),
            "minimum_block_cash_blocked_events": int(block_rows["blocked_cash_events"].max()),
            "gate_passed": gate_passed,
        },
        "decision": "promote_only_if_gate_passes",
        "candidate_promoted_to_canary": False,
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
