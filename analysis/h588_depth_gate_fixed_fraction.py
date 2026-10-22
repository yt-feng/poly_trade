"""H588: H578 entry-depth gate combined with fixed 75% cash sizing.

The signal and causal exit contract remain H434/H561.  Unlike H578's
piecewise sizing rule, H588 admits a row only when its entry-visible sell
capacity is at least five times the full-cash share requirement, then sizes
that row at a fixed 75% of currently reusable cash.  The rule is evaluated on
the pinned strict-quality old-clean archive with one evolving ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
import h508_cash_fraction_depth_tradeoff as h508
import h561_online_depth_proxy as h561

HYPOTHESIS_ID = "H588_H578_DEPTH_GATE_H452_FIXED_075"
RESULT_FORMAT = "old-clean-h588-depth-gate-fixed-fraction-v1"
SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
REQUIRED_FILLS = 10
FIXED_CASH_FRACTION = 0.75
DEPTH_RATIO_FLOOR = 5.0
SELECTION_WINDOWS = 8
LATENCIES_SECONDS = (2.0, 4.0, 8.0)
DEPTH_HAIRCUTS = (0.10, 0.15, 0.20, 0.25)


def depth_capacity_ratio(row: Any, cash: float) -> float:
    """Return entry sell depth divided by the full-cash share requirement."""
    full = h508._size_from_cash(row, cash, 1.0)
    if full is None or float(full["shares"]) <= 0.0:
        return 0.0
    column = "sell_up_size_2m" if str(row.side) == "up" else "sell_down_size_2m"
    try:
        depth = float(getattr(row, column))
    except (AttributeError, TypeError, ValueError):
        return 0.0
    return depth / float(full["shares"])


def replay(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    *,
    latency_seconds: float,
    depth_haircut: float,
    exit_cache: dict[tuple[Any, ...], dict[str, Any] | None],
) -> dict[str, Any]:
    """Replay one chronological chain; cash is released before the next entry."""
    if candidates.empty:
        return {
            "candidate_rows": 0,
            "qualified_rows": 0,
            "fills": 0,
            "blocked_before_release": 0,
            "ending_cash": h561.INITIAL_CASH,
            "first_ten_fills": 0,
            "first_ten_ending_cash": h561.INITIAL_CASH,
            "first_ten_min_cash_before_entry": h561.INITIAL_CASH,
            "first_ten_passed": False,
            "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = float(h561.INITIAL_CASH)
    pending: tuple[pd.Timestamp, float] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    qualified_rows = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts)
        if pending is not None:
            if entry <= pending[0]:
                blocked_before_release += 1
                continue
            cash += pending[1]
            pending = None
        if cash < h561.MINIMUM_ORDER_USD - 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        ratio = depth_capacity_ratio(row, cash)
        if ratio < DEPTH_RATIO_FLOOR:
            continue
        qualified_rows += 1
        terms = h508._size_from_cash(row, cash, FIXED_CASH_FRACTION)
        if terms is None:
            failure_reason = "cash_fraction_cannot_fit_minimum_order"
            break
        key = (
            str(row.slug), str(row.side), round(float(terms["shares"]), 8),
            float(row.slippage), float(latency_seconds), float(depth_haircut),
        )
        if key not in exit_cache:
            exit_cache[key] = h508.depth._first_stressed_exit(
                row, quotes.get(str(row.slug)), shares=float(terms["shares"]),
                entry_outlay=float(terms["reserved"]), latency_seconds=float(latency_seconds),
                depth_haircut=float(depth_haircut),
            )
        exit_event = exit_cache[key]
        if exit_event is None:
            failure_reason = "no_full_size_break_even_exit_under_latency_depth_stress"
            break
        before = cash
        cash -= float(terms["reserved"])
        payout = float(exit_event["exit_payout"])
        pending = (pd.Timestamp(row.cash_release_ts), payout)
        chain.append({
            "run_name": str(row.run_name),
            "slug": str(row.slug),
            "cash_fraction": FIXED_CASH_FRACTION,
            "depth_capacity_ratio": ratio,
            "entry_ts": entry.isoformat(),
            "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
            "exit_ts": str(exit_event["exit_ts"]),
            "cash_before_entry": before,
            "cash_after_entry": cash,
            "payout": payout,
        })
        if len(chain) >= REQUIRED_FILLS:
            break
    if pending is not None:
        cash += pending[1]
    prefix = chain[:REQUIRED_FILLS]
    ending = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else h561.INITIAL_CASH
    )
    minimum_cash = min([h561.INITIAL_CASH] + [float(item["cash_before_entry"]) for item in prefix])
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= h561.MINIMUM_ORDER_USD
        and ending >= h561.INITIAL_CASH
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)),
        "qualified_rows": int(qualified_rows),
        "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release),
        "ending_cash": ending,
        "first_ten_fills": int(len(prefix)),
        "first_ten_ending_cash": ending,
        "first_ten_min_cash_before_entry": minimum_cash,
        "first_ten_passed": passed,
        "failure_reason": failure_reason,
    }


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    by_slippage, runs, feature_rows = h561._candidate_frames(source_root)
    all_candidates = pd.concat(
        [frame for frames in by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = h561._quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = h561.rolling_windows(runs[4:], window_runs=h561.WINDOW_RUNS, stride=h561.WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    for slippage in h561.SLIPPAGES:
        base = pd.concat(by_slippage[float(slippage)], ignore_index=True)
        for latency in LATENCIES_SECONDS:
            for haircut in DEPTH_HAIRCUTS:
                cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                for window in windows:
                    result = replay(
                        base, window["runs"], quotes, latency_seconds=latency,
                        depth_haircut=haircut, exit_cache=cache,
                    )
                    rows.append({
                        "slippage": float(slippage),
                        "latency_seconds": float(latency),
                        "depth_haircut": float(haircut),
                        "window_index": int(window["window_index"]),
                        **result,
                    })
    frame = pd.DataFrame(rows)
    selection = frame[frame["window_index"] <= SELECTION_WINDOWS]
    holdout = frame[frame["window_index"] > SELECTION_WINDOWS]
    result = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": feature_rows,
        "rule": {
            "signal": "frozen H434 high-confidence ExtraTrees",
            "depth_ratio": "entry sell_up_size_2m or sell_down_size_2m divided by full-cash share requirement",
            "depth_ratio_floor": DEPTH_RATIO_FLOOR,
            "fixed_cash_fraction": FIXED_CASH_FRACTION,
        },
        "protocol": {
            "initial_cash_usd": h561.INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "same_ledger": True,
            "no_cross_window_concatenation": True,
            "cash_release_delay_seconds": h508.CASH_RELEASE_SECONDS,
            "taker_fee_rate": h508.TAKER_FEE_RATE,
            "paper_only": True,
        },
        "selection": {
            "rows": int(len(selection)),
            "all_rows_passed": bool(selection["first_ten_passed"].all()),
            "min_cash": float(selection["first_ten_ending_cash"].min()),
            "min_fills": int(selection["first_ten_fills"].min()),
        },
        "holdout": {
            "rows": int(len(holdout)),
            "all_rows_passed": bool(holdout["first_ten_passed"].all()),
            "min_cash": float(holdout["first_ten_ending_cash"].min()),
            "median_cash": float(holdout["first_ten_ending_cash"].median()),
            "min_fills": int(holdout["first_ten_fills"].min()),
        },
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "strict-quality old-clean paper replay; depth gate is observed at entry but execution, settlement and reusable cash remain unverified",
    }
    output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output / "rolling-metrics.csv", index=False)
    encoded = json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n"
    result["summary_sha256"] = hashlib.sha256(encoded).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps({key: result[key] for key in ("selection", "holdout")}, sort_keys=True))


if __name__ == "__main__":
    main()
