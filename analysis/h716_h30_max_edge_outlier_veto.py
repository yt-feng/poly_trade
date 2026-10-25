"""H716: isolate a fixed maximum-edge outlier veto on the frozen H30 signal.

The public stale-quote hypothesis says that unusually large model/book gaps can
be adverse-selection or stale-data outliers.  H408 combined that idea with a
favorite-side gate, a price band, a lower edge floor and dual-beta fair value;
it therefore could not identify which condition caused the chain to collapse.
H716 changes exactly one admission condition: retain the frozen H30 side and
timing, but reject only rows whose fixed dual-beta fee-adjusted edge exceeds
0.25.  There is no lower edge floor, favorite gate, price band, refit, data
collection, or order submission.  Selection and holdout remain separate
chronological ledgers and the ten-fill result is paper-only.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h406_old_clean_h30_quality_prefix import (
    HOLDOUT_BLOCK_SIZES,
    SOURCE_COMMIT,
    CANDIDATE as H30_CANDIDATE,
    ENTRY_MINUTE,
    FEE_RATE,
    FRACTION,
    HIGH_THRESHOLD,
    INITIAL_CASH,
    LOW_THRESHOLD,
    MINIMUM_ORDER,
    chronological_blocks,
)
from h53_prefix_only_compound_audit import add_frozen_candidates
from sequential_compound_gate import evaluate_compound_chain


CANDIDATE = "h716_h30_max_edge_outlier_veto"
ROBUST_BETAS = (0.83, 1.36)
ROBUST_TAU_SECONDS = 180.0
MAX_EDGE_AFTER_FEE = 0.25
SLIPPAGES = (0.005, 0.010)
MIN_ORDER_SHARES = 5.0
REQUIRED_FILLS = 10


def _normal_cdf(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.map(
        lambda value: np.nan
        if pd.isna(value)
        else 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))
    )


def chosen_edge_mask(side: pd.Series, edge_after_fee: pd.Series) -> pd.Series:
    """The only new gate: finite chosen-side edge must not exceed the cap."""
    return (
        side.astype(str).isin(("buy_up", "buy_down"))
        & pd.to_numeric(edge_after_fee, errors="coerce").notna()
        & pd.to_numeric(edge_after_fee, errors="coerce").le(MAX_EDGE_AFTER_FEE + 1e-12)
    ).fillna(False)


def add_candidate(features: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply H30 unchanged, then one fixed maximum-edge veto."""
    h30 = add_frozen_candidates(features)
    out = h30.copy()
    side = out[f"{H30_CANDIDATE}_side"].fillna("skip").astype(str)
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    sigma = pd.to_numeric(out["realized_vol_first2m"], errors="coerce")
    denom = sigma * math.sqrt(ROBUST_TAU_SECONDS)
    z = move.where(denom > 0).div(denom.where(denom > 0))
    robust_up = pd.concat(
        [_normal_cdf(z * beta) for beta in ROBUST_BETAS], axis=1
    ).min(axis=1, skipna=False)
    up_price = pd.to_numeric(out["buy_up_price_2m"], errors="coerce")
    down_price = pd.to_numeric(out["buy_down_price_2m"], errors="coerce")
    up_fee = FEE_RATE * up_price * (1.0 - up_price)
    down_fee = FEE_RATE * down_price * (1.0 - down_price)
    up_edge = robust_up - up_price - up_fee
    down_edge = (1.0 - robust_up) - down_price - down_fee
    edge = pd.Series(np.nan, index=out.index, dtype=float)
    edge.loc[side.eq("buy_up")] = up_edge.loc[side.eq("buy_up")]
    edge.loc[side.eq("buy_down")] = down_edge.loc[side.eq("buy_down")]
    eligible = chosen_edge_mask(side, edge)
    out[f"{CANDIDATE}_side"] = side.where(eligible, "skip")
    out[f"{CANDIDATE}_minute"] = ENTRY_MINUTE
    out[f"{CANDIDATE}_fraction"] = np.where(eligible, FRACTION, 0.0)
    out[f"{CANDIDATE}_component"] = "frozen_h30_plus_max_edge_veto"
    diagnostics = pd.DataFrame(
        {
            "h30_side": side,
            "robust_up_probability": robust_up,
            "edge_after_fee": edge,
            "eligible": eligible,
        }
    )
    return out, diagnostics


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


def replay(frame: pd.DataFrame, names: list[str], slippage: float) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        CANDIDATE,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        600.0,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MIN_ORDER_SHARES,
    )
    events = paper_events(logs)
    prefix = evaluate_compound_chain(
        events[:REQUIRED_FILLS],
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
        required_fills=REQUIRED_FILLS,
        evidence_mode="paper_upper_bound",
        require_prior_cash_release=True,
    )
    full = evaluate_compound_chain(
        events,
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
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
    frame, diagnostics = add_candidate(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits.extend(
        (f"holdout_block_{index}", block)
        for index, block in enumerate(chronological_blocks(holdout_runs), start=1)
    )
    rows: list[dict[str, Any]] = []
    for split, names in splits:
        for slippage in SLIPPAGES:
            rows.append({"split": split, "slippage": float(slippage), **replay(frame, names, slippage)})
    result = pd.DataFrame(rows)
    selection = result[result["split"].eq("selection20")]
    holdout = result[result["split"].eq("holdout59")]
    selection_passed = bool(
        len(selection) == len(SLIPPAGES)
        and selection["first_ten_passed"].all()
        and selection["first_ten_fills"].eq(REQUIRED_FILLS).all()
        and selection["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    holdout_passed = bool(
        len(holdout) == len(SLIPPAGES)
        and holdout["first_ten_passed"].all()
        and holdout["first_ten_fills"].eq(REQUIRED_FILLS).all()
        and holdout["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h716-h30-max-edge-outlier-veto-v1",
        "hypothesis_id": "H716_H30_MAX_EDGE_OUTLIER_VETO",
        "source_root": str(source_root), "source_commit": source_commit,
        "observed_source_commit": observed, "complete_runs": len(runs),
        "feature_rows": len(features), "coverage_rows": len(coverage),
        "strict_quality": True, "candidate": CANDIDATE,
        "eligible_signal_rows": int(diagnostics["eligible"].sum()),
        "h30_signal_rows": int(diagnostics["h30_side"].isin(("buy_up", "buy_down")).sum()),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "selection_prefix_passed": selection_passed, "holdout_prefix_passed": holdout_passed,
        "holdout_block_diagnostics": result[result["split"].str.startswith("holdout_block_")].to_dict(orient="records"),
        "fixed_gate": {
            "h30_signal_unchanged": True, "robust_betas": list(ROBUST_BETAS),
            "robust_tau_seconds": ROBUST_TAU_SECONDS,
            "max_edge_after_fee": MAX_EDGE_AFTER_FEE,
            "new_gate_only": "finite chosen-side fee-adjusted edge <= 0.25",
            "favorite_gate": False, "price_band_gate": False, "lower_edge_floor": False,
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "minimum_order_notional_usd": MINIMUM_ORDER,
            "minimum_order_shares": MIN_ORDER_SHARES, "taker_fee_rate": FEE_RATE,
            "entry_minute": ENTRY_MINUTE, "fraction": FRACTION,
            "cash_release_delay_seconds": 600.0, "prior_cash_release_required": True,
            "slippages": list(SLIPPAGES), "same_ledger": True,
            "no_concatenation": True, "paper_only": True,
        },
        "decision": "admit_for_receipt_join" if selection_passed and holdout_passed else "reject_h716_max_edge_veto",
        "new_data_collected": False, "model_refit": False,
        "live_orders_enabled": False, "paper_only": True,
        "actual_fills": 0, "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    diagnostics.to_csv(output / "entry_diagnostics.csv", index=False)
    coverage.to_csv(output / "coverage.csv", index=False)
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("decision", "h30_signal_rows", "eligible_signal_rows", "selection_prefix_passed", "holdout_prefix_passed")}, ensure_ascii=False, sort_keys=True))
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

