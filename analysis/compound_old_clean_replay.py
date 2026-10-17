"""Cloud-friendly old-clean replay under the single-account compound ledger.

The source contains paper labels rather than authenticated fills.  This
script therefore reports a paper upper bound separately and always leaves the
Canary gate false.  It uses the existing frozen strategy registry, cash lock,
fees, settlement delay and minimum order contract; it does not collect data or
submit orders.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from registry_small_cash_holdout import (
    DELAY_SECONDS,
    INITIAL_CASH,
    MINIMUM_ORDER,
    REGISTRY_NAMES,
    SLIPPAGE,
    TAKER_FEE_RATE,
    add_signals,
    prepare_registry_data,
)
from sequential_compound_gate import evaluate_compound_chain


def _seconds(value: object) -> str:
    return str(pd.Timestamp(value).timestamp())


def _paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    ordered = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": _seconds(row["first_quote_ts"]),
            "cash_available_ts": _seconds(row["release_ts"]),
            "entry_cost": str(row["filled_cost"]),
            "fee": str(row["fee_usd"]),
            "payout": str(row["payout_at_settlement"]),
        }
        for _, row in ordered.iterrows()
    ]


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = prepare_registry_data(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    rows: list[dict[str, object]] = []
    for strategy in REGISTRY_NAMES:
        frame = add_signals(features, strategy)
        for split, names in (
            ("selection20", selection_runs),
            ("holdout59", holdout_runs),
            ("all_test79", test_runs),
        ):
            logs, metrics = simulate_cash_locked(
                frame[frame["run_name"].isin(names)],
                strategy,
                study.ExecutionConfig(fixed_slippage=SLIPPAGE),
                INITIAL_CASH,
                MINIMUM_ORDER,
                DELAY_SECONDS,
                cooldown_events=0,
                capture_logs=True,
                fee_mode="polymarket_taker",
                fee_rate=TAKER_FEE_RATE,
            )
            chain = evaluate_compound_chain(
                _paper_events(logs),
                initial_cash=INITIAL_CASH,
                minimum_order=MINIMUM_ORDER,
                required_fills=10,
                evidence_mode="paper_upper_bound",
            )
            rows.append(
                {
                    "strategy": strategy,
                    "split": split,
                    "paper_fills": int(metrics["trades"]),
                    "paper_ending_cash": float(metrics["ending_cash"]),
                    "paper_min_cash_before_settlement": float(metrics["min_cash_before_settlement"]),
                    "paper_chain_fills": int(chain["fills"]),
                    "paper_chain_ending_cash": float(chain["ending_cash"]),
                    "paper_chain_minimum_cash": float(chain["minimum_cash"]),
                    "paper_chain_passed": bool(chain["paper_replay_passed"]),
                    "canary_gate_passed": bool(chain["gate_passed"]),
                    "chain_failure_reason": chain["failure_reason"],
                }
            )
    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "compound_metrics.csv", index=False)
    selection = result[result["split"].eq("selection20")].sort_values(
        ["paper_chain_passed", "paper_chain_fills", "paper_chain_ending_cash"],
        ascending=[False, False, False],
    )
    holdout = result[result["split"].eq("holdout59")]
    all_test = result[result["split"].eq("all_test79")]
    best_selection = selection.iloc[0].to_dict() if not selection.empty else None
    best_strategy = None if best_selection is None else str(best_selection["strategy"])
    selected_holdout = holdout[holdout["strategy"].eq(best_strategy)].iloc[0].to_dict() if best_strategy and not holdout[holdout["strategy"].eq(best_strategy)].empty else None
    selected_all = all_test[all_test["strategy"].eq(best_strategy)].iloc[0].to_dict() if best_strategy and not all_test[all_test["strategy"].eq(best_strategy)].empty else None
    meta = {
        "format": "old-clean-single-account-compound-replay-v1",
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "registry_count": len(REGISTRY_NAMES),
        "protocol": {
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "slippage": SLIPPAGE,
            "taker_fee_rate": TAKER_FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
        },
        "selected_by_paper_selection": best_strategy,
        "selected_holdout": selected_holdout,
        "selected_all_test": selected_all,
        "paper_only": True,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "canary_gate_passed": False,
        "evidence_boundary": "Paper labels and quote-derived execution only; no authenticated order, fill, settlement or reusable-cash receipt.",
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()
