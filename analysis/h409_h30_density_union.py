"""H409: density-preserving H30 union with a pre-registered fallback.

H30 remains the primary frozen signal.  Only rows where H30 skips may use the
already registered v1 adaptive signal with the existing path-efficiency state
``path_efficiency_first2m >= 0.50``.  This tests opportunity density without
changing the $10/$5 ledger, fee, settlement or slippage protocol.  It uses the
pinned strict-quality old-clean source, performs no fitting and submits no
orders.  A result is paper evidence only until an authenticated account bundle
is supplied.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h54_execution_contract_prefix_sensitivity as h54
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.07
CASH_RELEASE_DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
PATH_THRESHOLD = 0.50
H30_CANDIDATE = "h30_move_uncertainty"
FALLBACK_STRATEGY = "v1_adaptive_mix"
CANDIDATE = "h409_h30_density_union"
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def _paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ]


def add_candidate(features: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the frozen H30-primary, path-efficiency fallback union."""
    out = h54.add_frozen_candidates(features)
    h30_side = out[f"{H30_CANDIDATE}_side"].fillna("skip").astype(str)
    fallback_side = out[f"{FALLBACK_STRATEGY}_side"].fillna("skip").astype(str)
    path_ok = pd.to_numeric(out["path_efficiency_first2m"], errors="coerce").ge(PATH_THRESHOLD)
    fallback = h30_side.eq("skip") & path_ok & fallback_side.isin(("buy_up", "buy_down"))
    side = h30_side.where(h30_side.ne("skip"), fallback_side.where(fallback, "skip"))
    minute = out[f"{H30_CANDIDATE}_minute"].where(
        h30_side.ne("skip"), out[f"{FALLBACK_STRATEGY}_minute"].where(fallback, -1)
    )
    fraction = out[f"{H30_CANDIDATE}_fraction"].where(
        h30_side.ne("skip"), out[f"{FALLBACK_STRATEGY}_fraction"].where(fallback, 0.0)
    )
    out[f"{CANDIDATE}_side"] = side
    out[f"{CANDIDATE}_minute"] = pd.to_numeric(minute, errors="coerce").fillna(-1).astype(int)
    out[f"{CANDIDATE}_fraction"] = pd.to_numeric(fraction, errors="coerce").fillna(0.0)
    out[f"{CANDIDATE}_component"] = np.where(
        h30_side.ne("skip"), "h30_primary", np.where(fallback, "v1_path_fallback", "skip")
    )
    diagnostics = pd.DataFrame(
        {
            "h30_side": h30_side,
            "fallback_side": fallback_side,
            "path_ok": path_ok,
            "fallback_eligible": fallback,
            "final_side": side,
            "component": out[f"{CANDIDATE}_component"],
        }
    )
    return out, diagnostics


def replay(frame: pd.DataFrame, names: list[str], slippage: float, split: str) -> dict[str, Any]:
    subset = frame[frame["run_name"].astype(str).isin([str(name) for name in names])]
    logs, metrics = simulate_cash_locked(
        subset,
        CANDIDATE,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    events = _paper_events(logs)
    prefix = evaluate_compound_chain(
        events[:10],
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    component = subset[f"{CANDIDATE}_component"].astype(str)
    return {
        "split": split,
        "slippage": float(slippage),
        "eligible_signal_rows": int(subset[f"{CANDIDATE}_side"].astype(str).ne("skip").sum()),
        "h30_signal_rows": int(subset[f"{H30_CANDIDATE}_side"].astype(str).ne("skip").sum()),
        "fallback_signal_rows": int(component.eq("v1_path_fallback").sum()),
        "paper_trades": int(metrics["trades"]),
        "paper_ending_cash": float(metrics["ending_cash"]),
        "paper_min_cash_before_settlement": float(metrics["min_cash_before_settlement"]),
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash": float(prefix["ending_cash"]),
        "first_ten_minimum_cash": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def _blocks(runs: list[str]) -> list[list[str]]:
    if sum(HOLDOUT_BLOCK_SIZES) != len(runs):
        raise ValueError("HOLDOUT_BLOCK_SIZES_MUST_COVER_RUNS")
    result: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        result.append(runs[cursor : cursor + size])
        cursor += size
    return result


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame, diagnostics = add_candidate(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits.extend((f"holdout_block_{i}", block) for i, block in enumerate(_blocks(holdout_runs), 1))
    rows = [replay(frame, names, slip, split) for split, names in splits for slip in SLIPPAGES]
    result = pd.DataFrame(rows)
    selection = result[result["split"].eq("selection20")]
    holdout = result[result["split"].eq("holdout59")]
    selection_passed = bool(
        len(selection) == len(SLIPPAGES)
        and selection["first_ten_passed"].all()
        and selection["first_ten_fills"].eq(10).all()
        and selection["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    holdout_passed = bool(
        len(holdout) == len(SLIPPAGES)
        and holdout["first_ten_passed"].all()
        and holdout["first_ten_fills"].eq(10).all()
        and holdout["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    decision = "admit_for_receipt_join" if selection_passed and holdout_passed else "reject_h409_density_union"
    summary: dict[str, Any] = {
        "format": "old-clean-h409-h30-density-union-v1",
        "hypothesis_id": "H409_H30_PRIMARY_V1_PATH_DENSITY_UNION",
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
        "holdout_block_diagnostics": result[result["split"].str.startswith("holdout_block_")].to_dict(orient="records"),
        "density_union": {
            "primary": H30_CANDIDATE,
            "fallback": FALLBACK_STRATEGY,
            "fallback_filter": f"path_efficiency_first2m >= {PATH_THRESHOLD:.2f}",
            "precedence": "H30 first; fallback only when H30 skips",
            "no_refit": True,
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "decision": decision,
        "root_cause": (
            "The density union is admissible only if the fallback adds consecutive opportunities without "
            "breaking the same-ledger ten-fill prefix on untouched holdout."
        ),
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
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n")
    print(json.dumps({k: summary[k] for k in ("decision", "selection_prefix_passed", "holdout_prefix_passed")}, sort_keys=True))
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
