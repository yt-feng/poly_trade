"""Pre-registered ExtraTrees challenger on the pinned old-clean archive.

This is a paper replay only.  The model is fit causally on complete runs that
precede the scored run.  The replay is deliberately single-position: the next
entry is allowed only after the preceding market has settled and its cash has
been released.  This matches the continuous compounding Canary definition and
prevents two simultaneous $5 paper positions from being counted as a chain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
FEATURES = list(study.base.LOGIT_FEATURES)
MODEL_PARAMS: dict[str, Any] = {
    "n_estimators": 120,
    "max_depth": 7,
    "min_samples_leaf": 25,
    "max_features": 0.7,
    "class_weight": "balanced_subsample",
    "random_state": 4121,
    "n_jobs": -1,
}
SLIPPAGES = (0.005, 0.010)
INITIAL_CASH = 10.0
MINIMUM_ORDER_USD = 5.0
MINIMUM_ORDER_SHARES = 5.0
TAKER_FEE_RATE = 0.07
CASH_RELEASE_SECONDS = 900.0
ENTRY_SECONDS_AFTER_START = 124.0
EDGE_THRESHOLD = 0.02
PRICE_MIN = 0.05
PRICE_MAX = 0.80
MIN_DEPTH = 100.0
MAX_SPREAD = 0.06
MAX_OVERROUND = 0.03
REQUIRED_FILLS = 10


def _numeric(frame: pd.DataFrame, columns: list[str], medians: pd.Series | None = None) -> pd.DataFrame:
    out = frame[columns].replace([np.inf, -np.inf], np.nan)
    if medians is not None:
        out = out.fillna(medians)
    return out.fillna(0.0)


def _fit(train: pd.DataFrame) -> tuple[ExtraTreesClassifier | None, pd.Series]:
    medians = train[FEATURES].replace([np.inf, -np.inf], np.nan).median(numeric_only=True)
    X = _numeric(train, FEATURES, medians)
    y = pd.to_numeric(train["outcome_up"], errors="coerce").astype(int)
    if len(train) < 100 or y.nunique() < 2:
        return None, medians
    model = ExtraTreesClassifier(**MODEL_PARAMS)
    model.fit(X, y)
    return model, medians


def _candidate_frame(model: ExtraTreesClassifier | None, medians: pd.Series, current: pd.DataFrame, slippage: float) -> pd.DataFrame:
    if model is None or current.empty:
        return pd.DataFrame()
    scored = current.copy()
    scored["q_up"] = model.predict_proba(_numeric(scored, FEATURES, medians))[:, 1]
    scored["q_side"] = np.where(scored["q_up"] >= 0.5, scored["q_up"], 1.0 - scored["q_up"])
    scored["side"] = np.where(scored["q_up"] >= 0.5, "up", "down")
    scored["price"] = np.where(scored["side"].eq("up"), scored["buy_up_price_2m"], scored["buy_down_price_2m"])
    scored["depth"] = np.where(scored["side"].eq("up"), scored["buy_up_size_2m"], scored["buy_down_size_2m"])
    scored["spread"] = np.where(scored["side"].eq("up"), scored["spread_up_median_first2m"], scored["spread_down_median_first2m"])
    scored["price"] = pd.to_numeric(scored["price"], errors="coerce")
    scored["fill_price"] = scored["price"] + float(slippage)
    scored["shares"] = np.maximum(MINIMUM_ORDER_SHARES, MINIMUM_ORDER_USD / scored["fill_price"])
    scored["cost"] = scored["shares"] * scored["fill_price"]
    scored["fee"] = scored["shares"] * TAKER_FEE_RATE * scored["fill_price"] * (1.0 - scored["fill_price"])
    scored["edge"] = scored["q_side"] - scored["price"] - TAKER_FEE_RATE * scored["fill_price"] * (1.0 - scored["fill_price"])
    eligible = (
        scored["edge"].ge(EDGE_THRESHOLD)
        & scored["price"].between(PRICE_MIN, PRICE_MAX)
        & pd.to_numeric(scored["depth"], errors="coerce").ge(MIN_DEPTH)
        & pd.to_numeric(scored["spread"], errors="coerce").le(MAX_SPREAD)
        & pd.to_numeric(scored["overround_median_first2m"], errors="coerce").le(MAX_OVERROUND)
        & scored["fill_price"].lt(0.98)
        & (scored["cost"] + scored["fee"]).le(INITIAL_CASH)
    )
    scored = scored.loc[eligible].copy()
    if scored.empty:
        return scored
    scored["entry_ts"] = pd.to_datetime(scored["first_quote_ts"], utc=True) + pd.Timedelta(seconds=ENTRY_SECONDS_AFTER_START)
    epoch = pd.to_numeric(scored["slug"].astype(str).str.rsplit("-", n=1).str[-1], errors="coerce")
    scored["settlement_ts"] = pd.to_datetime(epoch, unit="s", utc=True, errors="coerce")
    scored["cash_release_ts"] = scored["settlement_ts"] + pd.Timedelta(seconds=CASH_RELEASE_SECONDS)
    scored["slippage"] = float(slippage)
    return scored.sort_values(["entry_ts", "slug"], kind="mergesort")


def replay_chain(candidates: pd.DataFrame, run_names: list[str]) -> dict[str, Any]:
    """Replay one strict, single-position, evolving-cash chain."""
    if candidates.empty:
        return {
            "candidate_rows": 0, "fills": 0, "blocked_before_release": 0,
            "ending_cash": INITIAL_CASH, "first_ten_fills": 0,
            "first_ten_ending_cash": INITIAL_CASH, "first_ten_wins": 0,
            "first_ten_min_cash_before_entry": INITIAL_CASH,
            "first_ten_passed": False, "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(["entry_ts", "slug"], kind="mergesort")
    cash = INITIAL_CASH
    previous: dict[str, Any] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = row.entry_ts.to_pydatetime()
        if previous is not None:
            if entry <= previous["cash_release_ts"]:
                blocked_before_release += 1
                continue
            cash += previous["payout"]
            if previous["payout"] <= 0:
                failure_reason = "prior_loss_ends_chain"
                break
        total = float(row.cost + row.fee)
        if total > cash + 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        before = cash
        cash -= total
        win = bool((float(row.outcome_up) == 1.0) if row.side == "up" else (float(row.outcome_up) == 0.0))
        payout = float(row.shares) if win else 0.0
        previous = {"cash_release_ts": row.cash_release_ts.to_pydatetime(), "payout": payout}
        chain.append({
            "slug": str(row.slug), "run_name": str(row.run_name), "entry_ts": row.entry_ts.isoformat(),
            "cash_release_ts": row.cash_release_ts.isoformat(), "side": str(row.side),
            "q_side": float(row.q_side), "price": float(row.price), "fill_price": float(row.fill_price),
            "cost": total, "fee": float(row.fee), "win": win,
            "cash_before_entry": before, "cash_after_entry": cash,
        })
        if len(chain) >= REQUIRED_FILLS:
            break
    if previous is not None:
        cash += previous["payout"]
    prefix = chain[:REQUIRED_FILLS]
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and all(item["win"] for item in prefix)
        and min(item["cash_before_entry"] for item in prefix) >= MINIMUM_ORDER_USD
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)), "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release), "ending_cash": float(cash),
        "first_ten_fills": int(len(prefix)),
        "first_ten_ending_cash": float(cash if len(prefix) == REQUIRED_FILLS else (prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH)),
        "first_ten_wins": int(sum(item["win"] for item in prefix)),
        "first_ten_min_cash_before_entry": float(min([INITIAL_CASH] + [item["cash_before_entry"] for item in prefix])),
        "first_ten_passed": passed, "failure_reason": failure_reason,
        "chain": prefix,
    }


def _strip_chain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_chain(item) for key, item in value.items() if key != "chain"}
    if isinstance(value, list):
        return [_strip_chain(item) for item in value]
    return value


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    features, coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    predictions: list[pd.DataFrame] = []
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        for slippage in SLIPPAGES:
            scored = _candidate_frame(model, medians, by_run[run_name], slippage)
            if not scored.empty:
                scored["model_run_index"] = index
                predictions.append(scored)
    all_candidates = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    holdout_blocks = [holdout_runs[i:i + 12] for i in range(0, len(holdout_runs), 12)]
    slippage_results: dict[str, Any] = {}
    for slippage in SLIPPAGES:
        candidates = all_candidates[all_candidates["slippage"].eq(slippage)] if not all_candidates.empty else pd.DataFrame()
        slippage_results[f"{slippage:.3f}"] = {
            "selection20": replay_chain(candidates, selection_runs),
            "holdout59": replay_chain(candidates, holdout_runs),
            "all_test79": replay_chain(candidates, test_runs),
            "holdout_blocks": [replay_chain(candidates, block) for block in holdout_blocks],
        }
    report: dict[str, Any] = {
        "format": "old-clean-h432-prequential-extratrees-strict-compound-v1",
        "hypothesis_id": "H432_PREQUENTIAL_EXTRATREES_STRICT_COMPOUND",
        "source_commit": source_commit,
        "source_root": str(source_root),
        "complete_runs": len(runs), "feature_rows": int(len(features)),
        "model": {"kind": "ExtraTreesClassifier", "features": FEATURES, "params": MODEL_PARAMS, "training_runs_strictly_before_current_run": True},
        "selection_rule": {"edge_threshold": EDGE_THRESHOLD, "price_min": PRICE_MIN, "price_max": PRICE_MAX, "min_depth": MIN_DEPTH, "max_spread": MAX_SPREAD, "max_overround": MAX_OVERROUND, "entry_seconds_after_start": ENTRY_SECONDS_AFTER_START, "fixed_slippages": list(SLIPPAGES)},
        "protocol": {"initial_cash_usd": INITIAL_CASH, "minimum_order_usd": MINIMUM_ORDER_USD, "minimum_order_shares": MINIMUM_ORDER_SHARES, "taker_fee_rate": TAKER_FEE_RATE, "cash_release_delay_seconds": CASH_RELEASE_SECONDS, "single_position_only": True, "prior_cash_release_required": True, "same_ledger": True, "no_reset": True, "no_concatenation": True, "required_consecutive_fills": REQUIRED_FILLS, "paper_only": True},
        "new_data_collected": False, "model_refit": True, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "slippages": {key: _strip_chain(value) for key, value in slippage_results.items()},
        "evidence_boundary": "strict-quality pinned old-clean paper replay; no authenticated order, fill, settlement, released-cash, or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    report["report_content_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"format": report["format"], "feature_rows": report["feature_rows"], "slippages": report["slippages"], "report_content_sha256": report["report_content_sha256"]}, ensure_ascii=False, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
