"""Verify the frozen H30 paper prefix on the strict-quality old-clean source.

H30 is already registered from the earlier old-clean frontier.  This replay
does not invent a new signal or tune a threshold: it reruns that one fixed
candidate after the quality-first source filter, with the same chronological
cash ledger and fee/minimum-order contract.  Selection and the untouched
holdout are evaluated separately; independent holdout blocks are diagnostics
and are never concatenated into the ten-fill result.  This remains paper-only.
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
from h53_prefix_only_compound_audit import (
    DELAY_SECONDS,
    FEE_RATE,
    FRACTION,
    HIGH_THRESHOLD,
    INITIAL_CASH,
    LOW_THRESHOLD,
    MINIMUM_ORDER,
    ENTRY_MINUTE,
    add_frozen_candidates,
    paper_events,
)
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CANDIDATE = "h30_move_uncertainty"
SLIPPAGES = (0.005, 0.010)
REQUIRED_FILLS = 10
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def chronological_blocks(runs: list[str]) -> list[list[str]]:
    if sum(HOLDOUT_BLOCK_SIZES) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def replay(frame: pd.DataFrame, run_names: list[str], slippage: float) -> dict[str, object]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in run_names])],
        CANDIDATE,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=5.0,
    )
    events = paper_events(logs)
    prefix = evaluate_compound_chain(
        events[:REQUIRED_FILLS],
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
    )
    full = evaluate_compound_chain(
        events,
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
    )
    return {
        "paper_trades": int(metrics["trades"]),
        "paper_ending_cash": float(metrics["ending_cash"]),
        "paper_min_cash_before_settlement": float(metrics["min_cash_before_settlement"]),
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash": float(prefix["ending_cash"]),
        "first_ten_minimum_cash": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
        "paper_chain_fills": int(full["fills"]),
        "paper_chain_ending_cash": float(full["ending_cash"]),
        "paper_chain_minimum_cash": float(full["minimum_cash"]),
        "paper_chain_passed": bool(full["paper_replay_passed"]),
        "paper_chain_failure_reason": full["failure_reason"],
    }


def evaluate(
    source_root: Path,
    *,
    source_commit: str,
    output: Path,
    max_snapshot_age_seconds: float = 90.0,
) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, coverage, runs = study.prepare_data(
        source_root,
        strict_quality=True,
        max_snapshot_age_seconds=float(max_snapshot_age_seconds),
    )
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [
        ("selection20", selection_runs),
        ("holdout59", holdout_runs),
    ]
    splits.extend(
        (f"holdout_block_{index}", block)
        for index, block in enumerate(chronological_blocks(holdout_runs), start=1)
    )
    rows: list[dict[str, object]] = []
    for split, names in splits:
        for slippage in SLIPPAGES:
            rows.append({"split": split, "slippage": float(slippage), **replay(frame, names, slippage)})
    result = pd.DataFrame(rows)
    selection = result[result["split"].eq("selection20")]
    holdout = result[result["split"].eq("holdout59")]
    selection_passed = bool(
        len(selection) == len(SLIPPAGES)
        and selection["first_ten_passed"].all()
        and selection["first_ten_fills"].eq(REQUIRED_FILLS).all()
        and selection["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    holdout_passed = bool(
        len(holdout) == len(SLIPPAGES)
        and holdout["first_ten_passed"].all()
        and holdout["first_ten_fills"].eq(REQUIRED_FILLS).all()
        and holdout["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    block_rows = result[result["split"].str.startswith("holdout_block_")]
    summary = {
        "format": "old-clean-h406-h30-quality-prefix-v1",
        "hypothesis_id": "H406_h30_strict_quality_prefix",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "coverage_rows": len(coverage),
        "strict_quality": True,
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "candidate": CANDIDATE,
        "selection_rule": "one frozen H30 candidate; selection must pass the exact ten-fill same-ledger prefix at both declared slippages before holdout is interpreted",
        "selection_prefix_passed": selection_passed,
        "holdout_prefix_passed": holdout_passed,
        "holdout_block_diagnostics": block_rows.to_dict(orient="records"),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER,
            "minimum_order_shares": 5.0,
            "taker_fee_rate": FEE_RATE,
            "fixed_entry_minute": ENTRY_MINUTE,
            "fixed_fraction": FRACTION,
            "fixed_low_threshold": LOW_THRESHOLD,
            "fixed_high_threshold": HIGH_THRESHOLD,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "decision": "admit_for_receipt_join" if selection_passed and holdout_passed else "reject_h406_strict_quality_prefix",
        "root_cause": (
            "The frozen H30 prefix passed the declared old-clean quality-first ten-fill gate."
            if selection_passed and holdout_passed
            else "The strict-quality source did not preserve a fee-adjusted ten-fill prefix in both selection and holdout."
        ),
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "canary_gate_passed": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    (output / "coverage.csv").write_text(coverage.to_csv(index=False), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("decision", "selection_prefix_passed", "holdout_prefix_passed", "feature_rows")}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-snapshot-age-seconds", type=float, default=90.0)
    args = parser.parse_args()
    evaluate(
        args.source_root,
        source_commit=args.source_commit,
        output=args.output,
        max_snapshot_age_seconds=args.max_snapshot_age_seconds,
    )


if __name__ == "__main__":
    main()
