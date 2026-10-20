"""H384 cash-release-delay sensitivity on the frozen old-clean archive.

This diagnostic changes one execution-clock input at a time.  The source
snapshot, registry, fees, slippage, minimum order and settlement time stay
fixed.  It evaluates one evolving paper cash ledger and never submits an
order.  A counterfactual result cannot open the authenticated Canary gate.
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

from cash_release_sensitivity import simulate_cash_locked
from registry_small_cash_holdout import (
    INITIAL_CASH,
    MINIMUM_ORDER,
    REGISTRY_NAMES,
    SELECTION_RUNS,
    SLIPPAGE,
    TAKER_FEE_RATE,
    add_signals,
    holdout_blocks,
    prepare_registry_data,
)
import v1_execution_walk_forward as study
from sequential_compound_gate import evaluate_compound_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
DELAYS_SECONDS = (0.0, 300.0, 600.0)
REQUIRED_FILLS = 10


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    ordered = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row["first_quote_ts"]).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row["release_ts"]).timestamp()),
            "entry_cost": str(row["filled_cost"]),
            "fee": str(row["fee_usd"]),
            "payout": str(row["payout_at_settlement"]),
        }
        for _, row in ordered.iterrows()
    ]


def replay(frame: pd.DataFrame, strategy: str, run_names: list[str], delay: float, split: str) -> dict[str, object]:
    working = frame[frame["run_name"].astype(str).isin([str(name) for name in run_names])]
    logs, metrics = simulate_cash_locked(
        working,
        strategy,
        study.ExecutionConfig(fixed_slippage=SLIPPAGE),
        INITIAL_CASH,
        MINIMUM_ORDER,
        delay,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=TAKER_FEE_RATE,
    )
    chain = evaluate_compound_chain(
        paper_events(logs),
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
        stop_after_required_fills=True,
    )
    return {
        "delay_seconds": float(delay),
        "strategy": strategy,
        "split": split,
        "paper_trades": int(metrics["trades"]),
        "paper_ending_cash": float(metrics["ending_cash"]),
        "paper_min_cash_before_settlement": float(metrics["min_cash_before_settlement"]),
        "chain_fills": int(chain["fills"]),
        "chain_ending_cash": float(chain["ending_cash"]),
        "chain_minimum_cash": float(chain["minimum_cash"]),
        "chain_paper_passed": bool(chain["paper_replay_passed"]),
        "chain_failure_reason": chain["failure_reason"],
    }


def evaluate(source_root: Path, output: Path, source_commit: str = SOURCE_COMMIT) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = prepare_registry_data(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    blocks = holdout_blocks(holdout_runs)
    frames = {strategy: add_signals(features, strategy) for strategy in REGISTRY_NAMES}

    rows: list[dict[str, object]] = []
    for delay in DELAYS_SECONDS:
        for strategy in REGISTRY_NAMES:
            rows.append(replay(frames[strategy], strategy, selection_runs, delay, "selection20"))
            rows.append(replay(frames[strategy], strategy, holdout_runs, delay, "holdout59"))
    table = pd.DataFrame(rows)
    selected: dict[str, str | None] = {}
    for delay in DELAYS_SECONDS:
        candidates = table[(table["delay_seconds"] == delay) & table["split"].eq("selection20")]
        eligible = candidates[(candidates["chain_fills"] >= REQUIRED_FILLS) & (candidates["chain_ending_cash"] >= INITIAL_CASH)]
        eligible = eligible.sort_values(["chain_ending_cash", "chain_fills", "strategy"], ascending=[False, False, True])
        selected[str(delay)] = None if eligible.empty else str(eligible.iloc[0]["strategy"])

    for delay in DELAYS_SECONDS:
        strategy = selected[str(delay)]
        if strategy is None:
            continue
        for index, names in enumerate(blocks, 1):
            rows.append(replay(frames[strategy], strategy, names, delay, f"holdout_block_{index}"))
    table = pd.DataFrame(rows)

    results: list[dict[str, object]] = []
    for delay in DELAYS_SECONDS:
        strategy = selected[str(delay)]
        result: dict[str, object] = {"delay_seconds": float(delay), "strategy": strategy}
        if strategy is not None:
            chosen = table[(table["delay_seconds"] == delay) & table["strategy"].eq(strategy)]
            selection = chosen[chosen["split"].eq("selection20")].iloc[0]
            holdout = chosen[chosen["split"].eq("holdout59")].iloc[0]
            blocks_table = chosen[chosen["split"].str.startswith("holdout_block_")]
            result.update(
                {
                    "selection": selection.to_dict(),
                    "holdout": holdout.to_dict(),
                    "blocks": blocks_table.to_dict("records"),
                    "all_blocks_10": bool(len(blocks_table) == 5 and (blocks_table["chain_fills"] >= REQUIRED_FILLS).all()),
                    "all_blocks_cash_floor_5": bool(len(blocks_table) == 5 and (blocks_table["chain_minimum_cash"] >= MINIMUM_ORDER).all()),
                    "all_blocks_growth": bool(len(blocks_table) == 5 and (blocks_table["chain_ending_cash"] >= INITIAL_CASH).all()),
                }
            )
            result["promotable_paper"] = bool(
                result["all_blocks_10"] and result["all_blocks_cash_floor_5"] and result["all_blocks_growth"]
            )
        results.append(result)

    summary: dict[str, object] = {
        "format": "h384-old-clean-cash-release-delay-sensitivity-v1",
        "hypothesis_id": "H384-old-clean-cash-release-delay-sensitivity",
        "source_commit": source_commit,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "registry_count": len(REGISTRY_NAMES),
        "delays_seconds": list(DELAYS_SECONDS),
        "selected_by_delay": selected,
        "results": results,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_usd": MINIMUM_ORDER,
            "taker_fee_rate": TAKER_FEE_RATE,
            "slippage": SLIPPAGE,
            "settlement_seconds": 300.0,
            "same_ledger": True,
            "required_fills": REQUIRED_FILLS,
            "paper_only": True,
        },
        "decision": "cash_release_delay_does_not_repair_selection_holdout_and_block_gate"
        if not any(bool(row.get("promotable_paper")) for row in results)
        else "counterfactual_candidate_requires_separate_review",
        "root_cause": "Reducing the release delay leaves the selected signal with the same holdout sparsity; the bottleneck is opportunity/regime transfer and fee-adjusted cash capacity, not this delay.",
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "existing old-clean quote-derived paper replay with a counterfactual cash clock; no authenticated order, fill, settlement or reusable-cash receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "results.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("selected_by_delay", "decision", "actual_fills")}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
