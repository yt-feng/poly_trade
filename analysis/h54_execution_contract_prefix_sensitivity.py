"""H54: execution-contract sensitivity for the exact ten-fill prefix.

The signal and thresholds are frozen from H53.  This audit uses the current
Polymarket CLOB contract documented for orders: the minimum is expressed in
shares (five shares in the documented example), while cash release remains an
explicit scenario because matching and on-chain settlement are asynchronous.
It evaluates only one evolving chronological ledger per split; independent
blocks are diagnostics and are never concatenated.  No new market data, refit,
or live order is used.
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
MINIMUM_ORDER_NOTIONAL = 0.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.07
SLIPPAGES = (0.005, 0.010)
DELAYS_SECONDS = (300.0, 600.0, 900.0)
ENTRY_MINUTE = 2
FRACTION = 0.04
LOW_THRESHOLD = -12.702899999999412
HIGH_THRESHOLD = 13.257220000004239
CANDIDATES = ("h30_move_uncertainty", "h38_move_uncertainty_midpath")
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
DOCS = {
    "place_orders": "https://docs.polymarket.com/trading/place-orders",
    "quickstart": "https://docs.polymarket.com/trading/quickstart",
    "documented_constraints": {
        "min_order_size_unit": "shares",
        "example_min_order_size": 5.0,
        "tick_size_required": True,
        "matching_and_settlement_async": True,
    },
}


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
        # The simulator enforces the documented share floor. This tiny positive
        # value only satisfies the generic ledger validator's required field.
        "minimum_order": "0.0000001",
        "required_fills": 10,
        "evidence_mode": "paper_upper_bound",
    }
    prefix = evaluate_compound_chain(events[:10], **kwargs)
    full = evaluate_compound_chain(events, **kwargs)
    return {
        "paper_chain_fills": int(full["fills"]),
        "paper_chain_ending_cash_usd": float(full["ending_cash"]),
        "paper_chain_passed": bool(full["paper_replay_passed"]),
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash_usd": float(prefix["ending_cash"]),
        "first_ten_minimum_cash_usd": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, candidate: str, names: list[str], slippage: float, delay: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        candidate,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        delay,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
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
    # Block rows are diagnostics at the conservative 600-second release only.
    splits.extend((f"holdout_block_{idx}", names) for idx, names in enumerate(chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES), start=1))
    rows: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for split, names in splits:
            delays = (600.0,) if split.startswith("holdout_block_") else DELAYS_SECONDS
            for delay in delays:
                for slippage in SLIPPAGES:
                    metrics, _logs = replay(frame, candidate, names, slippage, delay)
                    rows.append({"candidate": candidate, "split": split, "delay_seconds": delay, "slippage": slippage, **metrics})
    result = pd.DataFrame(rows)
    gates: dict[str, dict[str, object]] = {}
    for candidate in CANDIDATES:
        candidate_rows = result[result["candidate"].eq(candidate)]
        primary = candidate_rows[candidate_rows["split"].isin(["selection20", "holdout59"])]
        selection_rows = primary[primary["split"].eq("selection20")]
        holdout_rows = primary[primary["split"].eq("holdout59")]
        # The acceptance gate covers every declared delay and both declared
        # slippages. A single favorable 0.5% row is not sufficient.
        selection_pass = bool(
            len(selection_rows) == len(DELAYS_SECONDS) * len(SLIPPAGES)
            and selection_rows["first_ten_passed"].all()
            and (selection_rows["first_ten_fills"] == 10).all()
        )
        holdout_pass = bool(
            len(holdout_rows) == len(DELAYS_SECONDS) * len(SLIPPAGES)
            and holdout_rows["first_ten_passed"].all()
            and (holdout_rows["first_ten_fills"] == 10).all()
        )
        gates[candidate] = {
            "selection_prefix_all_delays_gate": selection_pass,
            "holdout_prefix_all_delays_gate": holdout_pass,
            "exact_ten_share_floor_gate": selection_pass and holdout_pass,
            "primary_rows": primary.to_dict(orient="records"),
            "holdout_block_600s_diagnostics": candidate_rows[candidate_rows["split"].str.startswith("holdout_block_")].to_dict(orient="records"),
        }
    summary = {
        "format": "old-clean-h54-execution-contract-prefix-sensitivity-v1",
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
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delays_seconds": list(DELAYS_SECONDS),
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "required_fills": 10,
            "tick_size_modeled": False,
        },
        "documentation_sources": DOCS,
        "primary_target":"exactly ten chronological fills with documented five-share minimum; no reset or block concatenation",
        "acceptance":"all declared delays and both slippages pass the ten-fill prefix on selection20 and holdout59",
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
    print(json.dumps({"gates": {k: v["exact_ten_share_floor_gate"] for k, v in gates.items()}}, ensure_ascii=False, sort_keys=True))
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
