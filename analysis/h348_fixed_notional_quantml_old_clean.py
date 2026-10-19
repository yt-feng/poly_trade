"""H348 fixed-notional replay for the previously zero-trade quantml family.

The earlier quantml walk-forward used a 1--2% fractional target and therefore
never reached the frozen $5 minimum order with a $10 account.  This audit keeps
the source snapshot, candidate definitions, causal time order, fee, settlement
and cash-release contract unchanged, but raises the target only to the minimum
admissible notional through ``simulate_cash_locked``.  Candidate thresholds are
computed from the first four complete runs, selected once on the next twenty,
and evaluated once on the remaining 59 runs.  The primary gate is ten
chronological fills on one evolving paper ledger; no blocks are concatenated.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
import sys

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
import quantml_candidate_execution_walk_forward as quantml
from cash_release_sensitivity import simulate_cash_locked
from h53_prefix_only_compound_audit import paper_events, chain_summary

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
FEE_RATE = 0.07
CASH_RELEASE_DELAY = 600.0
TRAIN_RUNS = 4
SELECTION_RUNS = 20
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
Q_VALUES = {"classic_early_drop_down": (None,), "btc2_path_eff": (0.85, 0.90, 0.95), "size_imbalance_uncertainty": (0.90, 0.95)}


def chronological_blocks(runs: list[str]) -> list[list[str]]:
    if sum(HOLDOUT_BLOCK_SIZES) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        out.append(runs[cursor : cursor + size])
        cursor += size
    return out


def add_signal(frame: pd.DataFrame, spec: quantml.CandidateSpec, hi: float, lo: float, strategy: str) -> pd.DataFrame:
    out = frame.copy()
    up, down, _ = quantml.signal_masks(out, spec, hi, lo)
    out[f"{strategy}_side"] = "skip"
    out.loc[up, f"{strategy}_side"] = "buy_up"
    out.loc[down, f"{strategy}_side"] = "buy_down"
    out[f"{strategy}_minute"] = int(spec.entry_minute)
    # The simulator raises this target to the $5 minimum when necessary; the
    # value is retained in the sealed report so sizing cannot silently revert
    # to the earlier 1--2% zero-trade configuration.
    out[f"{strategy}_fraction"] = 0.50
    return out


def replay(frame: pd.DataFrame, strategy: str, run_names: list[str], *, slippage: float = 0.0025) -> tuple[dict, pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in run_names])],
        strategy,
        study.ExecutionConfig(
            delay_seconds=4,
            max_spread=0.04,
            max_overround=0.03,
            min_depth=100.0,
            max_adverse_slippage=0.015,
            fixed_slippage=slippage,
        ),
        INITIAL_CASH,
        MINIMUM_ORDER,
        CASH_RELEASE_DELAY,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=5.0,
        single_position_only=True,
    )
    return {**metrics, **chain_summary(paper_events(logs))}, logs


def evaluate(source_root: Path, output: Path) -> dict:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != SOURCE_COMMIT:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{SOURCE_COMMIT}")
    features, _coverage, runs = quantml.prepare_complete_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    train = features[features["run_name"].astype(str).isin(runs[:TRAIN_RUNS])]
    selection_runs = runs[TRAIN_RUNS : TRAIN_RUNS + SELECTION_RUNS]
    holdout_runs = runs[TRAIN_RUNS + SELECTION_RUNS :]
    specs = quantml.candidate_registry()
    rows: list[dict] = []
    candidate_frames: dict[str, tuple[str, pd.DataFrame]] = {}
    for spec in specs:
        for q in Q_VALUES[spec.name]:
            hi, lo = quantml.factor_thresholds(train, spec, q)
            if spec.factor_col is not None and (not math.isfinite(hi) or not math.isfinite(lo)):
                continue
            q_label = "none" if q is None else str(q).replace(".", "")
            strategy = f"h348_{spec.name}_q{q_label}"
            frame = add_signal(features, spec, hi, lo, strategy)
            candidate_frames[strategy] = (spec.name, frame)
            for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs)):
                metrics, _logs = replay(frame, strategy, names)
                rows.append({"candidate": spec.name, "q_threshold": q, "hi_value": hi, "lo_value": lo, "strategy": strategy, "split": split, **metrics})
            for block_id, names in enumerate(chronological_blocks(holdout_runs), start=1):
                metrics, _logs = replay(frame, strategy, names)
                rows.append({"candidate": spec.name, "q_threshold": q, "hi_value": hi, "lo_value": lo, "strategy": strategy, "split": f"holdout_block_{block_id}", **metrics})
    result = pd.DataFrame(rows)
    selection = result[result["split"] == "selection20"].copy()
    eligible = selection[(selection["first_ten_fills"] == 10) & selection["first_ten_passed"] & (selection["first_ten_ending_cash_usd"] >= INITIAL_CASH)]
    choice = None if eligible.empty else eligible.sort_values(["first_ten_ending_cash_usd", "candidate"], ascending=[False, True]).iloc[0].to_dict()
    chosen_holdout = result[(result["split"] == "holdout59") & (result["strategy"] == (choice["strategy"] if choice else "__none__"))]
    holdout_passed = bool(not chosen_holdout.empty and (chosen_holdout.iloc[0]["first_ten_fills"] == 10) and chosen_holdout.iloc[0]["first_ten_passed"] and chosen_holdout.iloc[0]["first_ten_ending_cash_usd"] >= INITIAL_CASH)
    chosen_blocks = result[(result["split"].str.startswith("holdout_block_")) & (result["strategy"] == (choice["strategy"] if choice else "__none__"))]
    block_stability_passed = bool(
        choice
        and len(chosen_blocks) == len(HOLDOUT_BLOCK_SIZES)
        and (chosen_blocks["first_ten_fills"] == 10).all()
        and chosen_blocks["first_ten_passed"].all()
        and (chosen_blocks["first_ten_ending_cash_usd"] >= INITIAL_CASH).all()
    )
    report = {
        "format": "old-clean-h348-fixed-notional-quantml-v1",
        "hypothesis_id": "H348-FN",
        "source_commit": SOURCE_COMMIT,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidates": [spec.name for spec in specs],
        "training_runs": runs[:TRAIN_RUNS],
        "selection_runs": selection_runs,
        "holdout_runs": holdout_runs,
        "protocol": {"initial_cash_usd": INITIAL_CASH, "minimum_order_usd": MINIMUM_ORDER, "minimum_order_shares": 5.0, "fee_rate": FEE_RATE, "settlement_seconds": 300.0, "cash_release_delay_seconds": CASH_RELEASE_DELAY, "single_position_only": True, "same_ledger": True, "no_concatenation": True, "paper_only": True, "fixed_target_fraction": 0.50},
        "selection_eligible_count": int(len(eligible)),
        "selection_choice": choice,
        "holdout_passed": holdout_passed,
        "holdout_block_stability_passed": block_stability_passed,
        "decision": "reject_no_stable_fixed_notional_candidate" if not (holdout_passed and block_stability_passed) else "paper_candidate_only_receipt_gate_remains_closed",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "evidence_boundary": "pinned old-clean quote/depth paper replay; no authenticated order, fill, settlement or owner cash-release receipt",
        "metrics_rows": len(result),
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("selection_eligible_count", "holdout_passed", "decision", "actual_fills")}, ensure_ascii=False, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()
