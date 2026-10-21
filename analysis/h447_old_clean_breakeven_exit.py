"""H447: fixed fee-aware early-exit replay on the frozen H434 signal.

H441's only failing rolling window loses the first $5 taker position and leaves
less than the next minimum notional because the 7% crypto fee is charged at
entry.  H447 keeps the H434 model, entry gate, slippage cells, release clock
and one evolving $10 ledger fixed.  After a candidate is entered, it scans
only later quote rows for that market and exits the whole position at the
first observed bid that covers the entry outlay and the second taker fee.
If that causal break-even exit is never observed, the position is held to the
market's terminal outcome exactly as in H441.

This is a quote-replay upper bound, not an authenticated fill.  A quote bid
and displayed size do not prove queue position, order acceptance, a sell fill,
settlement, or reusable cash.  The replay deliberately keeps the original
900-second cash-release timestamp even after an early exit, so it does not
credit faster cash turnover that the archive cannot prove.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    CASH_RELEASE_SECONDS,
    EDGE_THRESHOLD,
    FEATURES,
    INITIAL_CASH,
    MAX_OVERROUND,
    MAX_SPREAD,
    MIN_DEPTH,
    MINIMUM_ORDER_SHARES,
    MINIMUM_ORDER_USD,
    MODEL_PARAMS,
    PRICE_MAX,
    PRICE_MIN,
    REQUIRED_FILLS,
    SLIPPAGES,
    TAKER_FEE_RATE,
    _candidate_frame,
    _fit,
    _numeric,
)
from h434_old_clean_extratrees_high_confidence import apply_confidence_gate
from h440_h434_rolling_stability import rolling_windows


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
ENTRY_SECONDS_AFTER_START = 124.0
WINDOW_RUNS = 12
WINDOW_STRIDE = 4
ROBUST_WINDOW_FRACTION = 0.80
BASELINE_H441_PASSED_WINDOWS = 16
QUOTE_COLUMNS = [
    "ts_iso", "slug", "sell_up_cents", "sell_down_cents",
    "sell_up_size", "sell_down_size",
]


def _quote_index(source_root: Path, slugs: set[str]) -> dict[str, pd.DataFrame]:
    """Read only candidate slugs from the pinned raw quote archive."""
    if not slugs:
        return {}
    frames: list[pd.DataFrame] = []
    files = sorted(source_root.rglob("*_quotes.csv"))
    if not files:
        raise RuntimeError("QUOTE_ARCHIVE_EMPTY")
    for path in files:
        for chunk in pd.read_csv(path, usecols=QUOTE_COLUMNS, chunksize=200_000):
            selected = chunk[chunk["slug"].astype(str).isin(slugs)].copy()
            if selected.empty:
                continue
            selected["ts_iso"] = pd.to_datetime(selected["ts_iso"], utc=True, errors="coerce")
            frames.append(selected)
    if not frames:
        return {}
    quotes = pd.concat(frames, ignore_index=True)
    quotes = quotes.dropna(subset=["ts_iso", "slug"])
    return {
        str(slug): group.sort_values("ts_iso", kind="mergesort").reset_index(drop=True)
        for slug, group in quotes.groupby(quotes["slug"].astype(str), sort=False)
    }


def _first_break_even_exit(row: Any, quotes: pd.DataFrame | None) -> dict[str, Any]:
    """Find the first causal bid that covers both taker fees and full size."""
    if quotes is None or quotes.empty:
        return {"exit_used": False, "exit_payout": None, "exit_ts": None, "exit_fill_price": None}
    side = str(row.side)
    bid_column = "sell_up_cents" if side == "up" else "sell_down_cents"
    size_column = "sell_up_size" if side == "up" else "sell_down_size"
    entry = pd.Timestamp(row.entry_ts)
    market_end = pd.Timestamp(row.settlement_ts) + pd.Timedelta(seconds=300.0)
    observed = quotes[(quotes["ts_iso"] >= entry) & (quotes["ts_iso"] < market_end)].copy()
    if observed.empty:
        return {"exit_used": False, "exit_payout": None, "exit_ts": None, "exit_fill_price": None}
    shares = float(row.shares)
    entry_outlay = float(row.cost + row.fee)
    bid = pd.to_numeric(observed[bid_column], errors="coerce") / 100.0
    size = pd.to_numeric(observed[size_column], errors="coerce")
    exit_fill = bid - float(row.slippage)
    exit_fee = shares * TAKER_FEE_RATE * exit_fill * (1.0 - exit_fill)
    proceeds = shares * exit_fill - exit_fee
    eligible = (
        bid.notna() & size.notna() & size.ge(shares)
        & exit_fill.gt(0.0) & exit_fill.lt(1.0)
        & proceeds.ge(entry_outlay - 1e-9)
    )
    if not bool(eligible.any()):
        return {"exit_used": False, "exit_payout": None, "exit_ts": None, "exit_fill_price": None}
    index = eligible[eligible].index[0]
    return {
        "exit_used": True,
        "exit_payout": float(proceeds.loc[index]),
        "exit_ts": pd.Timestamp(observed.loc[index, "ts_iso"]).isoformat(),
        "exit_fill_price": float(exit_fill.loc[index]),
    }


def _attach_exit_events(candidates: pd.DataFrame, quotes: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    out = candidates.copy()
    events = [_first_break_even_exit(row, quotes.get(str(row.slug))) for row in out.itertuples(index=False)]
    event_frame = pd.DataFrame(events, index=out.index)
    for column in event_frame.columns:
        out[column] = event_frame[column]
    return out


def _replay(candidates: pd.DataFrame, run_names: list[str], *, early_exit: bool) -> dict[str, Any]:
    """Replay one continuous ledger; an exit is a cash payout, not a new entry."""
    if candidates.empty:
        return {
            "candidate_rows": 0, "fills": 0, "blocked_before_release": 0,
            "ending_cash": INITIAL_CASH, "first_ten_fills": 0,
            "first_ten_ending_cash": INITIAL_CASH, "first_ten_wins": 0,
            "first_ten_losses": 0, "first_ten_early_exits": 0,
            "first_ten_min_cash_before_entry": INITIAL_CASH,
            "first_ten_passed": False, "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = INITIAL_CASH
    previous: dict[str, Any] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts).to_pydatetime()
        if previous is not None:
            if entry <= previous["cash_release_ts"]:
                blocked_before_release += 1
                continue
            cash += previous["payout"]
            previous = None
        total = float(row.cost + row.fee)
        if total > cash + 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        before = cash
        cash -= total
        final_win = bool(
            (float(row.outcome_up) == 1.0) if row.side == "up" else (float(row.outcome_up) == 0.0)
        )
        exit_used = bool(early_exit and row.exit_used)
        payout = float(row.exit_payout) if exit_used else (float(row.shares) if final_win else 0.0)
        previous = {
            "cash_release_ts": pd.Timestamp(row.cash_release_ts).to_pydatetime(),
            "payout": payout,
        }
        chain.append({
            "slug": str(row.slug), "run_name": str(row.run_name),
            "entry_ts": pd.Timestamp(row.entry_ts).isoformat(),
            "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
            "side": str(row.side), "q_side": float(row.q_side),
            "price": float(row.price), "fill_price": float(row.fill_price),
            "cost": total, "fee": float(row.fee), "final_outcome_win": final_win,
            "exit_used": exit_used, "exit_ts": str(row.exit_ts) if exit_used else None,
            "exit_fill_price": float(row.exit_fill_price) if exit_used else None,
            "payout": payout, "cash_before_entry": before, "cash_after_entry": cash,
        })
        if len(chain) >= REQUIRED_FILLS:
            break
    if previous is not None:
        cash += previous["payout"]
    prefix = chain[:REQUIRED_FILLS]
    prefix_ending_cash = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH
    )
    minimum_cash = float(min([INITIAL_CASH] + [item["cash_before_entry"] for item in prefix]))
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= MINIMUM_ORDER_USD
        and prefix_ending_cash >= INITIAL_CASH
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)), "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release), "ending_cash": float(cash),
        "first_ten_fills": int(len(prefix)), "first_ten_ending_cash": prefix_ending_cash,
        "first_ten_wins": int(sum(item["final_outcome_win"] for item in prefix)),
        "first_ten_losses": int(sum(not item["final_outcome_win"] for item in prefix)),
        "first_ten_early_exits": int(sum(item["exit_used"] for item in prefix)),
        "first_ten_min_cash_before_entry": minimum_cash,
        "first_ten_passed": passed, "failure_reason": failure_reason,
        "chain": prefix,
    }


def _strip_chain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_chain(item) for key, item in value.items() if key != "chain"}
    if isinstance(value, list):
        return [_strip_chain(item) for item in value]
    return value


def _cell_summary(rows: list[dict[str, Any]], slippage: float, candidate: str) -> dict[str, Any]:
    selected = [row for row in rows if float(row["slippage"]) == float(slippage) and row["candidate"] == candidate]
    passed = [row for row in selected if bool(row["first_ten_passed"])]
    return {
        "candidate": candidate, "slippage": float(slippage), "window_count": len(selected),
        "growth_positive_windows": len(passed),
        "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
        "robust_window_gate": bool(selected) and len(passed) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "all_windows_passed": bool(selected) and len(passed) == len(selected),
        "minimum_first_ten_cash_usd": min((float(row["first_ten_ending_cash"]) for row in selected), default=INITIAL_CASH),
        "median_first_ten_cash_usd": float(pd.Series([float(row["first_ten_ending_cash"]) for row in selected]).median()) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
        "total_first_ten_early_exits": int(sum(int(row["first_ten_early_exits"]) for row in selected)),
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
            candidate = apply_confidence_gate(_candidate_frame(model, medians, by_run[run_name], float(slippage)))
            if not candidate.empty:
                current = candidate.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    all_candidates = pd.concat(
        [frame for frames in candidates_by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str)))
    exit_candidates_by_slippage: dict[float, pd.DataFrame] = {}
    for slippage in SLIPPAGES:
        frames = candidates_by_slippage[float(slippage)]
        base = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        exit_candidates_by_slippage[float(slippage)] = _attach_exit_events(base, quotes)

    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    for window in windows:
        for slippage in SLIPPAGES:
            candidates = exit_candidates_by_slippage[float(slippage)]
            for label, early_exit in (("H441_BASELINE_NO_EARLY_EXIT", False), ("H447_BREAKEVEN_EXIT", True)):
                result = _replay(candidates, window["runs"], early_exit=early_exit)
                rows.append({
                    "window_index": window["window_index"], "start_run": window["start_run"],
                    "end_run": window["end_run"], "split": "h447_old_clean_breakeven_exit_rolling_window",
                    "candidate": label, "slippage": float(slippage), **_strip_chain(result),
                })
    cells = [
        _cell_summary(rows, float(slippage), "H447_BREAKEVEN_EXIT")
        for slippage in SLIPPAGES
    ]
    baseline_cells = [
        _cell_summary(rows, float(slippage), "H441_BASELINE_NO_EARLY_EXIT")
        for slippage in SLIPPAGES
    ]
    all_cells_pass = bool(cells) and all(bool(cell["all_windows_passed"]) for cell in cells)
    all_cells_robust = bool(cells) and all(bool(cell["robust_window_gate"]) for cell in cells)
    result: dict[str, Any] = {
        "format": "old-clean-h447-h434-breakeven-exit-v1",
        "hypothesis_id": "H447_H434_CAUSAL_BREAKEVEN_EXIT",
        "source_root": str(source_root), "source_commit": source_commit,
        "observed_source_commit": observed, "strict_quality": True,
        "complete_runs": len(runs), "feature_rows": int(len(features)),
        "candidate": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE_WITH_FEE_AWARE_BREAKEVEN_EXIT",
        "selection_rule": {
            "entry_model": "frozen H434 prequential ExtraTrees; q_side >= 0.80; no refit beyond H434",
            "exit_rule": "first later observed sell-side bid with displayed size >= full position and net proceeds >= entry cost plus entry fee after exit fee and same slippage",
            "exit_uses_final_label": False, "threshold_grid": False,
        },
        "window_count": len(windows), "window_runs": WINDOW_RUNS, "window_stride": WINDOW_STRIDE,
        "cell_summaries": cells, "baseline_h441_cell_summaries": baseline_cells,
        "all_cells_robust": all_cells_robust, "all_cells_passed": all_cells_pass,
        "non_degrading_vs_h441": bool(cells) and all(
            int(cell["growth_positive_windows"]) >= BASELINE_H441_PASSED_WINDOWS for cell in cells
        ),
        "window_rows": rows,
        "quote_coverage": {"candidate_slugs": int(all_candidates["slug"].nunique()) if not all_candidates.empty else 0, "matched_slugs": len(quotes)},
        "decision": "paper_stability_supports_breakeven_exit_receipts_required" if all_cells_pass else "reject_h447_breakeven_exit_stability",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": MINIMUM_ORDER_USD, "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE, "cash_release_delay_seconds": CASH_RELEASE_SECONDS,
            "slippages": list(SLIPPAGES), "same_ledger_within_window": True,
            "early_exit_cash_release_acceleration": False, "quote_size_must_cover_full_position": True,
            "hold_to_resolution_if_no_break_even_exit": True, "old_clean_dataset_only": True, "paper_only": True,
        },
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": False, "model_refit": True, "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean quote replay; displayed bid/size is not an authenticated sell fill and does not prove owner settlement or reusable cash",
        "root_cause": "H441's weak window is caused by the taker fee turning a nominal $5 loss into a cash balance below the next $5 minimum; a causal fee-aware exit can convert a later favorable bid into reusable proceeds without using the final label",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"window_count": len(windows), "cell_summaries": cells, "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
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
