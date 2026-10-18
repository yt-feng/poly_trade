"""Full cash-release counterfactual for the frozen H38 signal.

H38's signal, thresholds, fee/slippage contract and chronological run split
are fixed.  Only the cash-release delay changes.  The old-clean source is
read-only and no model is fitted here; every result is a paper replay on one
evolving cash ledger and remains outside the authenticated Canary gate.
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
from sequential_compound_gate import evaluate_compound_chain


STRATEGY = "h38_move_uncertainty_midpath"
SLIPPAGES = (0.005, 0.010)
DELAYS_SECONDS = (0.0, 300.0, 600.0, 900.0, 1500.0)
BLOCK_SIZES = (12, 12, 12, 12, 11)
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
FRACTION = 0.04
FROZEN_LOW_THRESHOLD = -12.702899999999412
FROZEN_HIGH_THRESHOLD = 13.257220000004239


def add_frozen_signal(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    probability = pd.to_numeric(out["mid_up_prob_2m"], errors="coerce")
    path = pd.to_numeric(out["path_efficiency_first2m"], errors="coerce")
    score = move * (1.0 - 2.0 * (probability - 0.5).abs())
    eligible = path.ge(0.25) & path.lt(0.50)
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[eligible & score.ge(FROZEN_HIGH_THRESHOLD)] = "buy_up"
    side.loc[eligible & score.le(FROZEN_LOW_THRESHOLD)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out[f"{STRATEGY}_minute"] = 2
    out[f"{STRATEGY}_fraction"] = FRACTION
    out[f"{STRATEGY}_component"] = "frozen_h38_move_uncertainty_midpath"
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    rows = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in rows.itertuples(index=False)
    ]


def replay(frame: pd.DataFrame, run_names: list[str], delay: float, slippage: float) -> tuple[dict, pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(run_names)],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=slippage),
        INITIAL_CASH,
        MINIMUM_ORDER,
        delay,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
    )
    chain = evaluate_compound_chain(
        paper_events(logs),
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    prefix = evaluate_compound_chain(
        paper_events(logs)[:10],
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        **metrics,
        "paper_chain_fills": chain["fills"],
        "paper_chain_ending_cash": chain["ending_cash"],
        "paper_chain_minimum_cash": chain["minimum_cash"],
        "paper_chain_passed": chain["paper_replay_passed"],
        "failure_reason": chain["failure_reason"],
        "first_ten_fills": prefix["fills"],
        "first_ten_ending_cash": prefix["ending_cash"],
        "first_ten_minimum_cash": prefix["minimum_cash"],
        "first_ten_passed": prefix["paper_replay_passed"],
    }, logs


def evaluate(source_root: Path, *, source_commit: str) -> dict:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, _, runs = study.prepare_data(source_root)
    frame = add_frozen_signal(frame)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    results: list[dict] = []
    blocks: list[dict] = []
    for delay in DELAYS_SECONDS:
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            for slippage in SLIPPAGES:
                metrics, logs = replay(frame, names, delay, slippage)
                results.append({"delay_seconds": delay, "split": split, "slippage": slippage, **metrics})
        cursor = 0
        for block, size in enumerate(BLOCK_SIZES, start=1):
            names = holdout_runs[cursor : cursor + size]
            cursor += size
            for slippage in SLIPPAGES:
                metrics, _ = replay(frame, names, delay, slippage)
                blocks.append({"delay_seconds": delay, "block": block, "run_count": size, "first_run": names[0], "last_run": names[-1], "slippage": slippage, **metrics})
    results_df = pd.DataFrame(results)
    blocks_df = pd.DataFrame(blocks)
    baseline = results_df[(results_df["delay_seconds"] == 600.0) & (results_df["split"] == "holdout59")]
    return {
        "format": "old-clean-h41-h38-release-delay-full-replay-v1",
        "candidate": "H38_move_uncertainty_midpath",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "frozen_signal": {
            "score": "btc_move_2m * (1 - 2*abs(mid_up_prob_2m - 0.5))",
            "low_threshold": FROZEN_LOW_THRESHOLD,
            "high_threshold": FROZEN_HIGH_THRESHOLD,
            "path_gate": "0.25 <= path_efficiency_first2m < 0.50",
            "entry_minute": 2,
            "fraction": FRACTION,
        },
        "protocol": {
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "delays_seconds": list(DELAYS_SECONDS),
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
        },
        "hypothesis": "If the 600-second cash-release delay is the dominant H38 failure, shortening it to settlement-only should restore ten consecutive holdout fills and all five blocks above the $5 floor.",
        "results": results,
        "blocks": blocks,
        "baseline_holdout_at_600_seconds": baseline.to_dict(orient="records"),
        "interpretation": "execution-state counterfactual only; no new data, no refit, no order and no authenticated Canary evidence",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    base = [row for row in result["results"] if row["split"] == "holdout59" and row["slippage"] == 0.005]
    print(json.dumps({"delays": [{"delay": row["delay_seconds"], "fills": row["trades"], "ending_cash": row["ending_cash"], "paper_chain_fills": row["paper_chain_fills"]} for row in base]}, sort_keys=True))


if __name__ == "__main__":
    main()
