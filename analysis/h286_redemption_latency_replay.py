"""H286: stress the frozen H30 paper ledger with observed redemption latency.

H284 found real historical redemption cash returning to the wallet only after
hours, while the old-clean paper contract used a 600-second release delay.
This diagnostic changes only that release clock.  The signal, source commit,
fee, minimum order, depth, slippage, and one-position ledger remain frozen.
It is a causal execution-contract test, not a strategy promotion.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h270_single_position_cash_turnover import chronological_blocks, DEPTH_FLOORS, HOLDOUT_BLOCK_SIZES
from h53_prefix_only_compound_audit import (
    INITIAL_CASH,
    MINIMUM_ORDER,
    SLIPPAGES,
    add_frozen_candidates,
    chain_summary,
    paper_events,
)

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
DELAYS_SECONDS = (600.0, 21_600.0, 28_800.0)
CANDIDATE = "h30_move_uncertainty"


def _replay(frame: pd.DataFrame, names: list[str], delay: float, slippage: float, depth: float) -> dict[str, object]:
    subset = frame[frame["run_name"].astype(str).isin([str(name) for name in names])]
    logs, metrics = simulate_cash_locked(
        subset,
        CANDIDATE,
        study.ExecutionConfig(fixed_slippage=float(slippage), min_depth=float(depth)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        delay,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        min_order_shares=5.0,
        single_position_only=True,
    )
    return {"delay_seconds": float(delay), "slippage": float(slippage), "depth_floor": float(depth), **metrics, **chain_summary(paper_events(logs))}


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits += [(f"holdout_block_{i}", block) for i, block in enumerate(chronological_blocks(holdout_runs), 1)]
    rows: list[dict[str, object]] = []
    for delay in DELAYS_SECONDS:
        for split, names in splits:
            cells = (
                [(slippage, depth) for slippage in SLIPPAGES for depth in DEPTH_FLOORS]
                if split in {"selection20", "holdout59"}
                else list(zip(SLIPPAGES, DEPTH_FLOORS))
            )
            for slippage, depth in cells:
                rows.append({"split": split, **_replay(frame, names, delay, slippage, depth)})
    summary = {
        "format": "old-clean-h286-redemption-latency-replay-v1",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": CANDIDATE,
        "hypothesis": "observed multi-hour redemption latency, rather than 600-second release, is the binding small-cash capacity constraint",
        "delays_seconds": list(DELAYS_SECONDS),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_usd": MINIMUM_ORDER,
            "minimum_order_shares": 5.0,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "single_position_only": True,
            "same_ledger": True,
            "no_concatenation": True,
            "no_refit": True,
            "paper_only": True,
        },
        "rows": len(rows),
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "frozen old-clean paper replay; no authenticated order, fill, settlement or owner cash-release admission",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "metrics.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "computed", "format": summary["format"], "rows": len(rows), "live_orders_enabled": False}, sort_keys=True))
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
