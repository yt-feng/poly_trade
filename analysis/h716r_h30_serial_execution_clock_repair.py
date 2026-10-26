"""H716R: causal repair of H716's serial cash-ledger execution clock.

This is a protocol/implementation repair, not a new model.  It keeps the
frozen H30 signal and H716 edge cap, but requires one unsettled position at a
time and timestamps both entry and cash release from the actual delayed
execution quote.  The old H716 result remains immutable and is not reused as
evidence for this repair.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h716_h30_max_edge_outlier_veto as base
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain


CANDIDATE = base.CANDIDATE
HYPOTHESIS_ID = "H716R_H30_SERIAL_EXECUTION_CLOCK_REPAIR"
ENTRY_CLOCK = "execution_quote_ts"
REQUIRED_FILLS = base.REQUIRED_FILLS


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    rows = logs.sort_values("entry_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.entry_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in rows.itertuples(index=False)
    ]


def replay(frame: pd.DataFrame, names: list[str], slippage: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        CANDIDATE,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        base.INITIAL_CASH,
        base.MINIMUM_ORDER,
        600.0,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=base.FEE_RATE,
        min_order_shares=base.MIN_ORDER_SHARES,
        single_position_only=True,
        entry_clock=ENTRY_CLOCK,
    )
    events = paper_events(logs)
    prefix = evaluate_compound_chain(
        events[:REQUIRED_FILLS],
        initial_cash=base.INITIAL_CASH,
        minimum_order=base.MINIMUM_ORDER,
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
        require_prior_cash_release=True,
    )
    full = evaluate_compound_chain(
        events,
        initial_cash=base.INITIAL_CASH,
        minimum_order=base.MINIMUM_ORDER,
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
        require_prior_cash_release=True,
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
        "entry_clock": metrics["entry_clock"],
        "blocked_entry_clock_events": int(metrics["blocked_entry_clock_events"]),
        "blocked_single_position_events": int(metrics["blocked_single_position_events"]),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame, diagnostics = base.add_candidate(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits.extend(
        (f"holdout_block_{index}", block)
        for index, block in enumerate(base.chronological_blocks(holdout_runs), start=1)
    )
    rows: list[dict[str, Any]] = []
    for split, names in splits:
        for slippage in base.SLIPPAGES:
            rows.append({"split": split, "slippage": float(slippage), **replay(frame, names, slippage)})
    result = pd.DataFrame(rows)
    selection = result[result["split"].eq("selection20")]
    holdout = result[result["split"].eq("holdout59")]
    selection_passed = bool(
        len(selection) == len(base.SLIPPAGES)
        and selection["first_ten_passed"].all()
        and selection["first_ten_fills"].eq(REQUIRED_FILLS).all()
        and selection["first_ten_ending_cash"].ge(base.INITIAL_CASH).all()
    )
    holdout_passed = bool(
        len(holdout) == len(base.SLIPPAGES)
        and holdout["first_ten_passed"].all()
        and holdout["first_ten_fills"].eq(REQUIRED_FILLS).all()
        and holdout["first_ten_ending_cash"].ge(base.INITIAL_CASH).all()
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h716r-h30-serial-execution-clock-repair-v1",
        "hypothesis_id": HYPOTHESIS_ID,
        "parent_hypothesis_id": "H716_H30_MAX_EDGE_OUTLIER_VETO",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "coverage_rows": len(coverage),
        "strict_quality": True,
        "candidate": CANDIDATE,
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "selection_prefix_passed": selection_passed,
        "holdout_prefix_passed": holdout_passed,
        "fixed_model_unchanged": True,
        "protocol": {
            "initial_cash_usd": base.INITIAL_CASH,
            "minimum_order_notional_usd": base.MINIMUM_ORDER,
            "minimum_order_shares": base.MIN_ORDER_SHARES,
            "taker_fee_rate": base.FEE_RATE,
            "cash_release_delay_seconds": 600.0,
            "same_ledger": True,
            "single_position_only": True,
            "entry_clock": ENTRY_CLOCK,
            "prior_cash_release_required": True,
            "slippages": list(base.SLIPPAGES),
            "no_concatenation": True,
            "paper_only": True,
        },
        "decision": "retain_repaired_contract_for_cloud_readback" if selection_passed and holdout_passed else "reject_h716r_serial_clock_repair",
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    diagnostics.to_csv(output / "entry_diagnostics.csv", index=False)
    coverage.to_csv(output / "coverage.csv", index=False)
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("decision", "selection_prefix_passed", "holdout_prefix_passed")}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=base.SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
