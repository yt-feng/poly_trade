"""Audit the exact user target: ten chronological fills on one cash ledger.

H53 does not alter either frozen signal.  It replays the already-registered H30
and H38 candidates on the old-clean monthly_runs snapshot and evaluates only the
first ten chronological paper fills as the primary target.  Full-chain and
per-block rows remain diagnostics; independent blocks are never concatenated.
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
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
DELAY_SECONDS = 600.0
FEE_RATE = 0.07
SLIPPAGES = (0.005, 0.010)
ENTRY_MINUTE = 2
FRACTION = 0.04
LOW_THRESHOLD = -12.702899999999412
HIGH_THRESHOLD = 13.257220000004239
CANDIDATES = ("h30_move_uncertainty", "h38_move_uncertainty_midpath")
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out: list[list[str]] = []
    cursor = 0
    for size in sizes:
        out.append(runs[cursor : cursor + size])
        cursor += size
    return out


def add_frozen_candidates(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    probability = pd.to_numeric(out["mid_up_prob_2m"], errors="coerce")
    path = pd.to_numeric(out["path_efficiency_first2m"], errors="coerce")
    score = move * (1.0 - 2.0 * (probability - 0.5).abs())
    h30_side = pd.Series("skip", index=out.index, dtype="object")
    h30_side.loc[score.ge(HIGH_THRESHOLD)] = "buy_up"
    h30_side.loc[score.le(LOW_THRESHOLD)] = "buy_down"
    h38_side = pd.Series("skip", index=out.index, dtype="object")
    eligible = path.ge(0.25) & path.lt(0.50)
    h38_side.loc[eligible & score.ge(HIGH_THRESHOLD)] = "buy_up"
    h38_side.loc[eligible & score.le(LOW_THRESHOLD)] = "buy_down"
    for name, side in ((CANDIDATES[0], h30_side), (CANDIDATES[1], h38_side)):
        out[f"{name}_side"] = side
        out[f"{name}_minute"] = ENTRY_MINUTE
        out[f"{name}_fraction"] = FRACTION
        out[f"{name}_component"] = f"frozen_{name}"
    out["h53_score"] = score
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


def chain_summary(events: list[dict[str, str]]) -> dict[str, object]:
    kwargs = {
        "initial_cash": str(INITIAL_CASH),
        "minimum_order": str(MINIMUM_ORDER),
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


def replay(frame: pd.DataFrame, candidate: str, run_names: list[str], slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in run_names])],
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
    )
    return {**metrics, **chain_summary(paper_events(logs))}, logs


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
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)]
    splits.extend((f"holdout_block_{idx}", names) for idx, names in enumerate(chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES), start=1))
    rows: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for split, names in splits:
            for slippage in SLIPPAGES:
                metrics, _logs = replay(frame, candidate, names, slippage)
                rows.append({"candidate": candidate, "split": split, "slippage": slippage, **metrics})
    result = pd.DataFrame(rows)
    primary = result[result["slippage"].eq(SLIPPAGES[0])]
    gates: dict[str, dict[str, object]] = {}
    for candidate in CANDIDATES:
        selection = primary[(primary.candidate == candidate) & primary.split.eq("selection20")].iloc[0]
        holdout = primary[(primary.candidate == candidate) & primary.split.eq("holdout59")].iloc[0]
        selection_pass = bool(selection.first_ten_passed and selection.first_ten_fills == 10 and selection.first_ten_ending_cash_usd >= INITIAL_CASH)
        holdout_pass = bool(holdout.first_ten_passed and holdout.first_ten_fills == 10 and holdout.first_ten_ending_cash_usd >= INITIAL_CASH)
        gates[candidate] = {
            "selection_prefix_gate": selection_pass,
            "holdout_prefix_gate": holdout_pass,
            "exact_ten_same_ledger_gate": selection_pass and holdout_pass,
            "selection20": selection.to_dict(),
            "holdout59": holdout.to_dict(),
            "holdout_block_prefix_diagnostics": primary[(primary.candidate == candidate) & primary.split.str.startswith("holdout_block_")].to_dict(orient="records"),
        }
    summary = {
        "format": "old-clean-h53-prefix-only-compound-audit-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
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
            "minimum_order_usd": MINIMUM_ORDER,
            "fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "required_fills": 10,
        },
        "primary_target": "exactly ten chronological fills from one evolving ledger; no reset and no concatenation of independent blocks",
        "full_chain_and_block_use": "diagnostics only; they do not change the exact-ten gate",
        "gates": gates,
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"gates": {k: v["exact_ten_same_ledger_gate"] for k, v in gates.items()}}, ensure_ascii=False, sort_keys=True))
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
