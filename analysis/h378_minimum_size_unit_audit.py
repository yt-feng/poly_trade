"""Old-clean audit of the exchange minimum-size unit ambiguity.

The frozen H30/H38 signals are replayed on the existing 83-run old-clean
snapshot.  The only changed variable is the execution contract: Gamma-style
notional floor, book-style share floor, or both.  Every result is evaluated on
one chronological cash ledger; independent blocks are diagnostics and are
never concatenated into a ten-fill result.

No data is collected, no model is refit, and no order is submitted.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h53_prefix_only_compound_audit import (
    CANDIDATES,
    DELAY_SECONDS,
    ENTRY_MINUTE,
    FEE_RATE,
    FRACTION,
    HIGH_THRESHOLD,
    HOLDOUT_BLOCK_SIZES,
    INITIAL_CASH,
    LOW_THRESHOLD,
    SLIPPAGES,
    add_frozen_candidates,
    chronological_blocks,
    paper_events,
)
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"

# These are deliberately explicit.  The first is the Gamma market-details
# interpretation, the second is the CLOB-book interpretation, and the third is
# the currently frozen replay contract.
CONTRACTS = {
    "gamma_notional_only": {"min_cost": 5.0, "min_order_shares": 0.0},
    "book_share_only": {"min_cost": 0.0, "min_order_shares": 5.0},
    "both_constraints": {"min_cost": 5.0, "min_order_shares": 5.0},
}


def chain_summary(events: list[dict[str, str]], minimum_order: float) -> dict[str, object]:
    kwargs = {
        "initial_cash": str(INITIAL_CASH),
        "minimum_order": str(max(float(minimum_order), 0.00000001)),
        "required_fills": 10,
        "evidence_mode": "paper_upper_bound",
    }
    full = evaluate_compound_chain(events, **kwargs)
    prefix = evaluate_compound_chain(events[:10], **kwargs)
    return {
        "paper_chain_fills": int(full["fills"]),
        "paper_chain_ending_cash_usd": float(full["ending_cash"]),
        "paper_chain_minimum_cash_usd": float(full["minimum_cash"]),
        "paper_chain_passed": bool(full["paper_replay_passed"]),
        "paper_chain_failure_reason": full["failure_reason"],
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash_usd": float(prefix["ending_cash"]),
        "first_ten_minimum_cash_usd": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(
    frame: pd.DataFrame,
    candidate: str,
    run_names: list[str],
    slippage: float,
    contract: dict[str, float],
) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in run_names])],
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        float(contract["min_cost"]),
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=float(contract["min_order_shares"]),
    )
    return {
        **metrics,
        **chain_summary(paper_events(logs), float(contract["min_cost"])),
    }, logs


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [
        ("selection20", selection_runs),
        ("holdout59", holdout_runs),
        ("all_test79", test_runs),
    ]
    splits.extend(
        (f"holdout_block_{idx}", names)
        for idx, names in enumerate(chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES), start=1)
    )
    rows: list[dict[str, object]] = []
    for contract_name, contract in CONTRACTS.items():
        for candidate in CANDIDATES:
            for split, names in splits:
                for slippage in SLIPPAGES:
                    metrics, _logs = replay(frame, candidate, names, slippage, contract)
                    rows.append(
                        {
                            "contract": contract_name,
                            "candidate": candidate,
                            "split": split,
                            "slippage": slippage,
                            **metrics,
                        }
                    )
    result = pd.DataFrame(rows)
    primary = result[result["slippage"].eq(SLIPPAGES[0])]
    gates: dict[str, dict[str, object]] = {}
    for contract_name, contract in CONTRACTS.items():
        contract_rows = primary[primary.contract.eq(contract_name)]
        candidates: dict[str, object] = {}
        for candidate in CANDIDATES:
            selection = contract_rows[
                (contract_rows.candidate == candidate) & contract_rows.split.eq("selection20")
            ].iloc[0]
            holdout = contract_rows[
                (contract_rows.candidate == candidate) & contract_rows.split.eq("holdout59")
            ].iloc[0]
            selection_pass = bool(
                selection.first_ten_passed
                and selection.first_ten_fills == 10
                and selection.first_ten_ending_cash_usd >= INITIAL_CASH
            )
            holdout_pass = bool(
                holdout.first_ten_passed
                and holdout.first_ten_fills == 10
                and holdout.first_ten_ending_cash_usd >= INITIAL_CASH
            )
            candidates[candidate] = {
                "selection_prefix_gate": selection_pass,
                "holdout_prefix_gate": holdout_pass,
                "exact_ten_same_ledger_gate": selection_pass and holdout_pass,
                "selection20": selection.to_dict(),
                "holdout59": holdout.to_dict(),
                "holdout_block_prefix_diagnostics": contract_rows[
                    (contract_rows.candidate == candidate)
                    & contract_rows.split.str.startswith("holdout_block_")
                ].to_dict(orient="records"),
            }
        passing = [
            (name, data)
            for name, data in candidates.items()
            if data["selection_prefix_gate"]
        ]
        selected_candidate = None
        if passing:
            selected_candidate = max(
                passing,
                key=lambda item: float(item[1]["selection20"]["first_ten_ending_cash_usd"]),
            )[0]
        gates[contract_name] = {
            "min_order_notional_usd": float(contract["min_cost"]),
            "min_order_shares": float(contract["min_order_shares"]),
            "selected_by_selection_prefix": selected_candidate,
            "selected_candidate_exact_ten_same_ledger_gate": (
                None if selected_candidate is None else candidates[selected_candidate]["exact_ten_same_ledger_gate"]
            ),
            "candidates": candidates,
        }
    summary = {
        "format": "old-clean-h378-minimum-size-unit-audit-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "contracts": CONTRACTS,
        "candidates": list(CANDIDATES),
        "frozen_signal": {
            "score": "btc_move_2m * (1 - 2*abs(mid_up_prob_2m - 0.5))",
            "low_threshold": LOW_THRESHOLD,
            "high_threshold": HIGH_THRESHOLD,
            "h30_gate": "none",
            "h38_gate": "0.25 <= path_efficiency_first2m < 0.50",
            "entry_minute": ENTRY_MINUTE,
            "fraction": FRACTION,
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "required_fills": 10,
            "selection_runs": 20,
            "holdout_runs": 59,
            "blocks_are_diagnostics_only": True,
        },
        "primary_target": "exactly ten chronological fills from one evolving ledger; no reset and no concatenation of independent blocks",
        "hypothesis": "If the exchange contract is share-only rather than notional-only, a low-price share floor could materially change the small-account chain; if both apply, the current replay is unchanged.",
        "evidence_boundary": "paper_upper_bound_only",
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "gates": gates,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "contracts": {
                    name: data["selected_candidate_exact_ten_same_ledger_gate"]
                    for name, data in gates.items()
                }
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
