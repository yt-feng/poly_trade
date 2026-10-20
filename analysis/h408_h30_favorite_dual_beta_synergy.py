"""H408: frozen H30 plus favorite-side dual-beta edge synergy.

This is one predeclared, old-clean paper diagnostic motivated by the public
polybtc research review.  It keeps H30's signal thresholds, entry minute and
cash sizing fixed, then requires the H30 side to be the market favorite and to
clear a fixed dual-beta, fee-adjusted fair-value edge inside the documented
favorite price band.  No beta is fitted here, no data is collected, and no
orders are submitted.  Selection, holdout and each holdout block are evaluated
as separate chronological ledgers; no block is concatenated into another.
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


CANDIDATE = "h408_h30_favorite_dual_beta_synergy"
ROBUST_BETAS = (0.83, 1.36)
ROBUST_TAU_SECONDS = 180.0
MIN_EDGE_AFTER_FEE = 0.02
MAX_EDGE_AFTER_FEE = 0.25
MIN_FAVORITE_PROBABILITY = 0.50
MIN_FAVORITE_PRICE = 0.50
MAX_FAVORITE_PRICE = 0.80
MIN_MARKET_DEVIATION = 0.20
SLIPPAGES = (0.005, 0.010)


def _normal_cdf(value: pd.Series) -> pd.Series:
    values = pd.to_numeric(value, errors="coerce")
    return values.map(
        lambda item: np.nan
        if pd.isna(item)
        else 0.5 * (1.0 + math.erf(float(item) / math.sqrt(2.0)))
    )


def add_candidate(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the fixed H30/favorite/dual-beta gate without fitting."""
    out = frame.copy()
    h30_side = out[f"{H30_CANDIDATE}_side"].fillna("skip").astype(str)
    market_prob = pd.to_numeric(out["mid_up_prob_2m"], errors="coerce")
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    sigma = pd.to_numeric(out["realized_vol_first2m"], errors="coerce")
    denom = sigma * math.sqrt(ROBUST_TAU_SECONDS)
    z = move.where(denom > 0).div(denom.where(denom > 0))
    robust_up = pd.concat([_normal_cdf(z * beta) for beta in ROBUST_BETAS], axis=1).min(axis=1, skipna=False)
    up_price = pd.to_numeric(out["buy_up_price_2m"], errors="coerce")
    down_price = pd.to_numeric(out["buy_down_price_2m"], errors="coerce")
    up_fee = FEE_RATE * up_price * (1.0 - up_price)
    down_fee = FEE_RATE * down_price * (1.0 - down_price)
    up_edge = robust_up - up_price - up_fee
    down_edge = (1.0 - robust_up) - down_price - down_fee

    favorite = ((h30_side == "buy_up") & market_prob.ge(MIN_FAVORITE_PROBABILITY)) | (
        (h30_side == "buy_down") & market_prob.le(1.0 - MIN_FAVORITE_PROBABILITY)
    )
    market_deviation = (market_prob - 0.5).abs()
    price = pd.Series(np.nan, index=out.index, dtype=float)
    edge = pd.Series(np.nan, index=out.index, dtype=float)
    price.loc[h30_side == "buy_up"] = up_price.loc[h30_side == "buy_up"]
    price.loc[h30_side == "buy_down"] = down_price.loc[h30_side == "buy_down"]
    edge.loc[h30_side == "buy_up"] = up_edge.loc[h30_side == "buy_up"]
    edge.loc[h30_side == "buy_down"] = down_edge.loc[h30_side == "buy_down"]
    eligible = (
        h30_side.isin(("buy_up", "buy_down"))
        & favorite
        & market_deviation.ge(MIN_MARKET_DEVIATION - 1e-12)
        & price.ge(MIN_FAVORITE_PRICE - 1e-12)
        & price.le(MAX_FAVORITE_PRICE + 1e-12)
        & edge.ge(MIN_EDGE_AFTER_FEE - 1e-12)
        & edge.le(MAX_EDGE_AFTER_FEE + 1e-12)
    )
    side = h30_side.where(eligible, "skip")
    out[f"{CANDIDATE}_side"] = side
    out[f"{CANDIDATE}_minute"] = ENTRY_MINUTE
    out[f"{CANDIDATE}_fraction"] = np.where(eligible, FRACTION, 0.0)
    out[f"{CANDIDATE}_component"] = "frozen_h30_favorite_dual_beta"
    diagnostics = pd.DataFrame(
        {
            "h30_side": h30_side,
            "market_probability": market_prob,
            "robust_up_probability": robust_up,
            "entry_price": price,
            "edge_after_fee": edge,
            "favorite": favorite,
            "market_deviation": market_deviation,
            "eligible": eligible,
        }
    )
    return out, diagnostics


def _paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
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
        min_order_shares=5.0,
    )
    events = _paper_events(logs)
    prefix = evaluate_compound_chain(
        events[:10],
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
        required_fills=10,
        evidence_mode="paper_upper_bound",
        require_prior_cash_release=True,
    )
    full = evaluate_compound_chain(
        events,
        initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER,
        required_fills=10,
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
        source_root,
        strict_quality=True,
        max_snapshot_age_seconds=90.0,
    )
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    h30 = add_frozen_candidates(features)
    frame, diagnostics = add_candidate(h30)
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
        and selection["first_ten_fills"].eq(10).all()
        and selection["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    holdout_passed = bool(
        len(holdout) == len(SLIPPAGES)
        and holdout["first_ten_passed"].all()
        and holdout["first_ten_fills"].eq(10).all()
        and holdout["first_ten_ending_cash"].ge(INITIAL_CASH).all()
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h408-h30-favorite-dual-beta-synergy-v1",
        "hypothesis_id": "H408_H30_FAVORITE_DUAL_BETA_SYNERGY",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "coverage_rows": len(coverage),
        "strict_quality": True,
        "candidate": CANDIDATE,
        "eligible_signal_rows": int(diagnostics["eligible"].sum()),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "selection_prefix_passed": selection_passed,
        "holdout_prefix_passed": holdout_passed,
        "holdout_block_diagnostics": result[result["split"].str.startswith("holdout_block_")].to_dict(orient="records"),
        "sources": {
            "github_repo": "https://github.com/LukasNSteel/polybtc",
            "github_strategy_claim": "favorite-side stale-quote sniper with dual-beta robust edge and 0.50-0.80 price band; treated as an unverified hypothesis for this old-clean replay",
        },
        "fixed_gate": {
            "h30_thresholds_unchanged": True,
            "robust_betas": list(ROBUST_BETAS),
            "robust_tau_seconds": ROBUST_TAU_SECONDS,
            "min_edge_after_fee": MIN_EDGE_AFTER_FEE,
            "max_edge_after_fee": MAX_EDGE_AFTER_FEE,
            "favorite_probability_cutoff": MIN_FAVORITE_PROBABILITY,
            "market_deviation_floor": MIN_MARKET_DEVIATION,
            "favorite_price_band": [MIN_FAVORITE_PRICE, MAX_FAVORITE_PRICE],
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER,
            "minimum_order_shares": 5.0,
            "taker_fee_rate": FEE_RATE,
            "entry_minute": ENTRY_MINUTE,
            "fraction": FRACTION,
            "cash_release_delay_seconds": 600.0,
            "prior_cash_release_required": True,
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "decision": "admit_for_receipt_join" if selection_passed and holdout_passed else "reject_h408_synergy",
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
    (output / "coverage.csv").write_text(coverage.to_csv(index=False), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("decision", "eligible_signal_rows", "selection_prefix_passed", "holdout_prefix_passed")}, sort_keys=True))
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
