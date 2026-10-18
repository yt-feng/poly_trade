"""H275: screen two new old-data candidates under the frozen cash ledger.

The candidates come from the completed all-monthly CLOB discovery run.  This
module is deliberately a second-stage screen: it keeps the pinned old-clean
source, the existing 4-second execution snapshots, 7% taker fee, five-share
minimum, 300-second settlement, 600-second cash release and strict
single-position ledger.  It never submits an order and its report is intended
to be sealed by the cloud workflow.
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

import all_monthly_clob_systematic_research_v2 as base
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h53_prefix_only_compound_audit import chain_summary, paper_events


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CANDIDATES = ("static_m4_breakout_up_tight", "state_selector_robust")
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
CASH_RELEASE_DELAY = 600.0
FEE_RATE = 0.07
SLIPPAGES = (0.005, 0.010)
DEPTH_FLOORS = (100.0, 150.0)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def chronological_blocks(runs: list[str]) -> list[list[str]]:
    if sum(HOLDOUT_BLOCK_SIZES) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    result: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        result.append(runs[cursor : cursor + size])
        cursor += size
    return result


def add_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    """Add only the two predeclared candidates to a prepared feature frame."""
    out = frame.copy()
    for candidate in CANDIDATES:
        out[f"{candidate}_side"] = "skip"
        out[f"{candidate}_minute"] = -1
        out[f"{candidate}_fraction"] = 0.10

    move4 = pd.to_numeric(out["btc_move_4m"], errors="coerce")
    size4 = pd.to_numeric(out["buy_up_size_4m"], errors="coerce")
    price4 = pd.to_numeric(out["buy_up_price_4m"], errors="coerce")
    static_mask = move4.gt(30) & move4.le(50) & size4.ge(100) & price4.le(0.90)
    out.loc[static_mask, "static_m4_breakout_up_tight_side"] = "buy_up"
    out.loc[static_mask, "static_m4_breakout_up_tight_minute"] = 4

    # The discovery implementation is prequential.  Use its incremental
    # history index so the strict screen does not refit or inspect the future.
    history = base.CandidateHistoryIndex()
    for index, row in out.iterrows():
        if index >= 60:
            choice = base.choose_trade(
                None, row, "state_selector_robust", 0.01, history_index=history
            )
            out.at[index, "state_selector_robust_side"] = choice["side"]
            out.at[index, "state_selector_robust_minute"] = int(choice["minute"])
        history.update(row)
    return out


def replay(
    frame: pd.DataFrame,
    candidate: str,
    run_names: list[str],
    slippage: float,
    depth_floor: float,
) -> dict[str, object]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin(run_names)],
        candidate,
        study.ExecutionConfig(
            fixed_slippage=float(slippage), min_depth=float(depth_floor)
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
    return {
        "candidate": candidate,
        "slippage": float(slippage),
        "depth_floor": float(depth_floor),
        **metrics,
        **chain_summary(paper_events(logs)),
    }


def evaluate(source_root: Path, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [
        ("selection20", selection_runs),
        ("holdout59", holdout_runs),
    ]
    splits.extend(
        (f"holdout_block_{index}", names)
        for index, names in enumerate(chronological_blocks(holdout_runs), start=1)
    )

    rows: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for split, names in splits:
            for slippage in SLIPPAGES:
                for depth_floor in DEPTH_FLOORS:
                    rows.append(
                        {
                            "split": split,
                            **replay(frame, candidate, names, slippage, depth_floor),
                        }
                    )

    selection_rows = [row for row in rows if row["split"] == "selection20"]
    eligible = []
    for candidate in CANDIDATES:
        candidate_rows = [
            row for row in selection_rows if row["candidate"] == candidate
        ]
        if (
            len(candidate_rows) == len(SLIPPAGES) * len(DEPTH_FLOORS)
            and all(
                int(row["first_ten_fills"]) == 10
                and bool(row["first_ten_passed"])
                for row in candidate_rows
            )
        ):
            eligible.append(
                {
                    "candidate": candidate,
                    "worst_first_ten_cash_usd": min(
                        float(row["first_ten_ending_cash_usd"])
                        for row in candidate_rows
                    ),
                }
            )
    choice = (
        sorted(eligible, key=lambda item: (-item["worst_first_ten_cash_usd"], item["candidate"]))[0]
        if eligible
        else None
    )
    chosen_holdout = [
        row
        for row in rows
        if choice
        and row["split"] == "holdout59"
        and row["candidate"] == choice["candidate"]
    ]
    report = {
        "format": "h275-old-clean-systematic-candidate-strict-replay-v1",
        "hypothesis_id": "H275",
        "source": {
            "root": str(source_root),
            "snapshot_commit": source_commit,
            "complete_runs": len(runs),
            "feature_rows": len(frame),
        },
        "candidates": list(CANDIDATES),
        "selection": {
            "rows": len(selection_rows),
            "eligible": eligible,
            "choice": choice,
        },
        "holdout": {
            "rows": len(chosen_holdout),
            "passed": bool(chosen_holdout)
            and all(
                int(row["first_ten_fills"]) == 10
                and bool(row["first_ten_passed"])
                and float(row["first_ten_ending_cash_usd"]) >= INITIAL_CASH
                for row in chosen_holdout
            ),
            "rows_all_candidates": len([row for row in rows if row["split"] == "holdout59"]),
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": 10,
            "minimum_order_shares": 5.0,
            "fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY,
            "slippages": list(SLIPPAGES),
            "depth_floors": list(DEPTH_FLOORS),
            "single_position_only": True,
            "same_ledger": True,
            "no_concatenation": True,
            "no_refit": True,
            "paper_only": True,
        },
        "metrics_rows": rows,
        "decision": {
            "old_data_strategy_evaluated": True,
            "old_data_candidate_promoted": False,
            "new_data_collected": False,
            "model_refit": False,
            "paper_strategy_dispatch": False,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
            "live_orders_enabled": False,
            "promotion_gate_passed": False,
            "reason": "Discovery candidates are admitted only if every selection cell has ten positive same-ledger fills; holdout is evaluated once and never tunes the choice.",
        },
        "evidence_boundary": "Pinned old-clean paper replay with existing execution snapshots; no authenticated order, fill, settlement or owner cash-release receipt.",
        "next_action": "If no candidate passes, close H275 and do not repeat quote-only scans. If one passes, keep it paper-only until an owner-scoped receipt bundle proves ten settled and cash-released fills.",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    pd.DataFrame(rows).to_csv(output / "metrics.csv", index=False)
    print(json.dumps({"status": "computed", "rows": len(rows)}, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.source_commit, args.output)


if __name__ == "__main__":
    main()
