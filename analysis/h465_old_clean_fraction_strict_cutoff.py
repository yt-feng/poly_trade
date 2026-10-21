"""H465: test the frozen H453 cash fraction under a stricter exit cutoff.

H453 selected the full available-cash fraction on the unchanged strict-quality
old-clean source.  H465 keeps that fraction, the prequential H434 entry gate,
fees, slippage cells, release clock and one evolving $10 ledger fixed.  It
requires a causal full-size break-even exit before market end minus 60 seconds
to test whether the paper edge depends on late displayed depth.

This is a paper replay only.  Displayed bids and sizes are not authenticated
fills, settlement receipts or reusable cash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    INITIAL_CASH,
    MINIMUM_ORDER_SHARES,
    MINIMUM_ORDER_USD,
    SLIPPAGES,
    TAKER_FEE_RATE,
    _candidate_frame,
    _fit,
)
from h434_old_clean_extratrees_high_confidence import apply_confidence_gate
from h440_h434_rolling_stability import rolling_windows
from h447_old_clean_breakeven_exit import (
    EXPECTED_COMPLETE_RUNS,
    SOURCE_COMMIT,
    WINDOW_RUNS,
    WINDOW_STRIDE,
    _quote_index,
)


GROSS_CASH_FRACTION = 1.0
EXIT_CUTOFF_SECONDS = 60.0
CASH_RELEASE_SECONDS = 900.0
REQUIRED_FILLS = 10


def _fee(shares: float, fill_price: float) -> float:
    return shares * TAKER_FEE_RATE * fill_price * (1.0 - fill_price)


def _size_from_cash(row: Any, cash: float) -> dict[str, float] | None:
    if cash < MINIMUM_ORDER_USD - 1e-12:
        return None
    fill_price = float(row.fill_price)
    if not 0.0 < fill_price < 1.0:
        return None
    gross_target = max(MINIMUM_ORDER_USD, GROSS_CASH_FRACTION * cash)
    shares = max(MINIMUM_ORDER_SHARES, gross_target / fill_price)
    per_share = fill_price + TAKER_FEE_RATE * fill_price * (1.0 - fill_price)
    if shares * per_share > cash + 1e-12:
        shares = min(shares, cash / per_share)
    reserved = shares * per_share
    if shares < MINIMUM_ORDER_SHARES - 1e-9:
        return None
    if shares * fill_price < MINIMUM_ORDER_USD - 1e-9:
        return None
    if reserved > cash + 1e-9:
        return None
    return {
        "shares": float(shares),
        "cost": float(shares * fill_price),
        "fee": float(_fee(shares, fill_price)),
        "reserved": float(reserved),
    }


def _first_break_even_exit(
    row: Any,
    quotes: pd.DataFrame | None,
    *,
    shares: float,
    entry_outlay: float,
) -> dict[str, Any] | None:
    """Find a full-size fee-aware exit strictly before market end minus 60s."""
    if quotes is None or quotes.empty:
        return None
    side = str(row.side)
    bid_column = "sell_up_cents" if side == "up" else "sell_down_cents"
    size_column = "sell_up_size" if side == "up" else "sell_down_size"
    entry = pd.Timestamp(row.entry_ts)
    market_end = pd.Timestamp(row.settlement_ts) + pd.Timedelta(seconds=300.0)
    cutoff = market_end - pd.Timedelta(seconds=EXIT_CUTOFF_SECONDS)
    observed = quotes[(quotes["ts_iso"] >= entry) & (quotes["ts_iso"] < cutoff)]
    if observed.empty:
        return None
    shares = float(shares)
    entry_outlay = float(entry_outlay)
    bid = pd.to_numeric(observed[bid_column], errors="coerce") / 100.0
    size = pd.to_numeric(observed[size_column], errors="coerce")
    exit_fill = bid - float(row.slippage)
    exit_fee = shares * TAKER_FEE_RATE * exit_fill * (1.0 - exit_fill)
    proceeds = shares * exit_fill - exit_fee
    eligible = (
        bid.notna()
        & size.notna()
        & size.ge(shares)
        & exit_fill.gt(0.0)
        & exit_fill.lt(1.0)
        & proceeds.ge(entry_outlay - 1e-9)
    )
    if not bool(eligible.any()):
        return None
    index = eligible[eligible].index[0]
    return {
        "exit_ts": pd.Timestamp(observed.loc[index, "ts_iso"]).isoformat(),
        "payout": float(proceeds.loc[index]),
    }


def _replay(candidates: pd.DataFrame, run_names: list[str], quotes: dict[str, pd.DataFrame]) -> dict[str, Any]:
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = float(INITIAL_CASH)
    pending: tuple[pd.Timestamp, float] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts)
        if pending is not None:
            if entry <= pending[0]:
                blocked_before_release += 1
                continue
            cash += pending[1]
            pending = None
        terms = _size_from_cash(row, cash)
        if terms is None:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        exit_event = _first_break_even_exit(
            row,
            quotes.get(str(row.slug)),
            shares=float(terms["shares"]),
            entry_outlay=float(terms["reserved"]),
        )
        if exit_event is None:
            failure_reason = "no_full_size_break_even_exit_before_60s_cutoff"
            break
        before = cash
        cash -= terms["reserved"]
        pending = (pd.Timestamp(row.cash_release_ts), float(exit_event["payout"]))
        chain.append({
            "entry_ts": entry.isoformat(),
            "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
            "cash_before_entry": before,
            "cash_after_entry": cash,
            "shares": terms["shares"],
            "cost": terms["cost"],
            "fee": terms["fee"],
            "payout": exit_event["payout"],
            "exit_ts": exit_event["exit_ts"],
        })
        if len(chain) >= REQUIRED_FILLS:
            break
    prefix = chain[:REQUIRED_FILLS]
    if pending is not None:
        cash += pending[1]
    ending = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH
    )
    min_before = min([INITIAL_CASH] + [float(row["cash_before_entry"]) for row in prefix])
    passed = bool(len(prefix) == REQUIRED_FILLS and min_before >= MINIMUM_ORDER_USD and ending >= INITIAL_CASH)
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)),
        "fills": int(len(prefix)),
        "blocked_before_release": int(blocked_before_release),
        "first_ten_fills": int(len(prefix)),
        "first_ten_ending_cash": ending,
        "first_ten_min_cash_before_entry": float(min_before),
        "first_ten_early_exits": int(len(prefix)),
        "first_ten_passed": passed,
        "failure_reason": failure_reason,
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    candidates_by_slippage: dict[float, list[pd.DataFrame]] = {float(s): [] for s in SLIPPAGES}
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        if model is None:
            continue
        for slippage in SLIPPAGES:
            current = apply_confidence_gate(_candidate_frame(model, medians, by_run[run_name], float(slippage)))
            if not current.empty:
                current = current.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    all_candidates = pd.concat(
        [frame for frames in candidates_by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str))) if not all_candidates.empty else {}
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for slippage in SLIPPAGES:
        base = pd.concat(candidates_by_slippage[float(slippage)], ignore_index=True)
        selected: list[dict[str, Any]] = []
        for window in windows:
            result = _replay(base, window["runs"], quotes)
            row = {
                "slippage": float(slippage),
                "window_index": window["window_index"],
                "start_run": window["start_run"],
                "end_run": window["end_run"],
                **result,
            }
            rows.append(row)
            selected.append(row)
        passed = [row for row in selected if bool(row["first_ten_passed"])]
        cells.append({
            "slippage": float(slippage),
            "window_count": len(selected),
            "all_windows_passed": bool(selected) and len(passed) == len(selected),
            "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
            "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
            "minimum_first_ten_cash_usd": min((float(row["first_ten_ending_cash"]) for row in selected), default=INITIAL_CASH),
            "minimum_cash_before_entry_usd": min((float(row["first_ten_min_cash_before_entry"]) for row in selected), default=INITIAL_CASH),
            "total_first_ten_early_exits": sum(int(row["first_ten_early_exits"]) for row in selected),
        })
    all_pass = bool(cells) and all(cell["all_windows_passed"] for cell in cells)
    result: dict[str, Any] = {
        "format": "old-clean-h465-h453-strict-cutoff-v1",
        "hypothesis_id": "H465_H453_FULL_CASH_STRICT_60S_CUTOFF",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "candidate": "H453_H447_H434_FULL_CASH_FRACTION",
        "gross_cash_fraction": GROSS_CASH_FRACTION,
        "exit_cutoff_seconds_before_market_end": EXIT_CUTOFF_SECONDS,
        "window_count": len(windows),
        "cell_summaries": cells,
        "all_cells_passed": all_pass,
        "decision": "h465_strict_cutoff_passes_paper_gate" if all_pass else "h465_strict_cutoff_rejects_or_weakens_h453",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_SECONDS,
            "same_ledger_within_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "new_market_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean quote replay; displayed bid/size is not authenticated execution or reusable cash",
        "root_cause": "tests whether the H453 paper chain survives removal of the final 60 seconds of displayed depth",
        "window_rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cell_summaries": cells, "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
