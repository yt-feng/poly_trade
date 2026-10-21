"""H542: pre-registered cash-fraction boost after observed profitable exits.

H534's fixed 0.55 fraction is the incumbent paper profile.  H542 tests one
online policy on the same strict-quality old-clean archive: start at 0.55 and
use 0.60 only after two consecutive *strictly profitable* released exits.  A
non-profitable or break-even exit resets the next entry to 0.55.  The policy
uses only information available before the next entry and keeps the existing
fee, latency, depth, release and one-ledger contract unchanged.
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

import h508_cash_fraction_depth_tradeoff as h508
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
from h447_old_clean_breakeven_exit import (
    EXPECTED_COMPLETE_RUNS,
    SOURCE_COMMIT,
    WINDOW_RUNS,
    WINDOW_STRIDE,
    _quote_index,
)
from h440_h434_rolling_stability import rolling_windows
from h452_old_clean_cash_fraction import _size_from_cash


HYPOTHESIS_ID = "H542_ADAPTIVE_CASH_BOOST_AFTER_TWO_PROFITS"
RESULT_FORMAT = "old-clean-h542-adaptive-cash-boost-v1"
BASE_FRACTION = 0.55
BOOST_FRACTION = 0.60
BOOST_AFTER_PROFITS = 2
REQUIRED_FILLS = 10
LATENCIES_SECONDS = (2.0, 4.0, 8.0)
DEPTH_HAIRCUTS = (0.10, 0.15, 0.20, 0.25)


def _fraction_for_state(
    consecutive_profits: int,
    *,
    base_fraction: float = BASE_FRACTION,
    boost_fraction: float = BOOST_FRACTION,
    boost_after_profits: int = BOOST_AFTER_PROFITS,
) -> float:
    """Return the next-entry fraction from already released outcomes only."""
    if not 0.0 < float(base_fraction) <= 1.0:
        raise ValueError("base_fraction must be in (0, 1]")
    if not 0.0 < float(boost_fraction) <= 1.0:
        raise ValueError("boost_fraction must be in (0, 1]")
    if int(boost_after_profits) < 1:
        raise ValueError("boost_after_profits must be positive")
    return float(boost_fraction if int(consecutive_profits) >= int(boost_after_profits) else base_fraction)


def _replay_adaptive(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    *,
    latency_seconds: float,
    depth_haircut: float,
    exit_cache: dict[tuple[Any, ...], dict[str, Any] | None],
    base_fraction: float = BASE_FRACTION,
    boost_fraction: float = BOOST_FRACTION,
    boost_after_profits: int = BOOST_AFTER_PROFITS,
) -> dict[str, Any]:
    """Replay H542 on one rolling window without resetting the cash ledger."""
    h508.depth._validate_stress(latency_seconds, depth_haircut)
    if candidates.empty:
        return {
            "candidate_rows": 0, "fills": 0, "blocked_before_release": 0,
            "ending_cash": INITIAL_CASH, "first_ten_fills": 0,
            "first_ten_ending_cash": INITIAL_CASH,
            "first_ten_min_cash_before_entry": INITIAL_CASH,
            "first_ten_passed": False, "failure_reason": "no_candidates",
            "boost_entries": 0, "profitable_exits": 0,
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = float(INITIAL_CASH)
    pending: tuple[pd.Timestamp, float] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    consecutive_profits = 0
    boost_entries = 0
    profitable_exits = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts)
        if pending is not None:
            if entry <= pending[0]:
                blocked_before_release += 1
                continue
            cash += pending[1]
            pending = None
        if cash < MINIMUM_ORDER_USD - 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        fraction = _fraction_for_state(
            consecutive_profits,
            base_fraction=base_fraction,
            boost_fraction=boost_fraction,
            boost_after_profits=boost_after_profits,
        )
        if fraction == float(boost_fraction):
            boost_entries += 1
        terms = _size_from_cash(row, cash, fraction)
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
                entry_outlay=float(terms["reserved"]), latency_seconds=latency_seconds,
                depth_haircut=depth_haircut,
            )
        exit_event = exit_cache[key]
        if exit_event is None:
            failure_reason = "no_full_size_break_even_exit_under_latency_depth_stress"
            break
        before = cash
        cash -= float(terms["reserved"])
        payout = float(exit_event["exit_payout"])
        profitable = payout > float(terms["reserved"]) + 1e-9
        if profitable:
            profitable_exits += 1
            consecutive_profits += 1
        else:
            consecutive_profits = 0
        pending = (pd.Timestamp(row.cash_release_ts), payout)
        chain.append({
            "run_name": str(row.run_name), "entry_ts": entry.isoformat(),
            "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
            "fraction": float(fraction), "shares": float(terms["shares"]),
            "cost": float(terms["cost"]), "fee": float(terms["fee"]),
            "payout": payout, "profitable_exit": bool(profitable),
            "exit_ts": str(exit_event["exit_ts"]),
            "cash_before_entry": before, "cash_after_entry": cash,
        })
        if len(chain) >= REQUIRED_FILLS:
            break
    if pending is not None:
        cash += pending[1]
    prefix = chain[:REQUIRED_FILLS]
    ending = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH
    )
    minimum_cash = min([INITIAL_CASH] + [float(item["cash_before_entry"]) for item in prefix])
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= MINIMUM_ORDER_USD
        and ending >= INITIAL_CASH
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)), "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release), "ending_cash": ending,
        "first_ten_fills": int(len(prefix)), "first_ten_ending_cash": ending,
        "first_ten_min_cash_before_entry": float(minimum_cash),
        "first_ten_passed": passed, "failure_reason": failure_reason,
        "boost_entries": int(boost_entries), "profitable_exits": int(profitable_exits),
        "chain": prefix,
    }


def _build_candidates(source_root: Path, runs: list[str], features: pd.DataFrame) -> dict[float, list[pd.DataFrame]]:
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
            candidate = apply_confidence_gate(
                _candidate_frame(model, medians, by_run[run_name], float(slippage))
            )
            if not candidate.empty:
                current = candidate.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    return candidates_by_slippage


def evaluate(
    source_root: Path,
    *,
    source_commit: str = SOURCE_COMMIT,
    output: Path,
    hypothesis_id: str = HYPOTHESIS_ID,
    result_format: str = RESULT_FORMAT,
    base_fraction: float = BASE_FRACTION,
    boost_fraction: float = BOOST_FRACTION,
    boost_after_profits: int = BOOST_AFTER_PROFITS,
) -> dict[str, Any]:
    _fraction_for_state(
        0,
        base_fraction=base_fraction,
        boost_fraction=boost_fraction,
        boost_after_profits=boost_after_profits,
    )
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    candidates_by_slippage = _build_candidates(source_root, runs, features)
    all_candidates = pd.concat(
        [frame for frames in candidates_by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    for slippage in SLIPPAGES:
        base = pd.concat(candidates_by_slippage[float(slippage)], ignore_index=True)
        for latency in LATENCIES_SECONDS:
            for haircut in DEPTH_HAIRCUTS:
                adaptive_cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                baseline_cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                for window in windows:
                    adaptive = _replay_adaptive(
                        base, window["runs"], quotes, latency_seconds=float(latency),
                        depth_haircut=float(haircut), exit_cache=adaptive_cache,
                        base_fraction=base_fraction, boost_fraction=boost_fraction,
                        boost_after_profits=boost_after_profits,
                    )
                    baseline = h508._replay_fraction_depth(
                        base, window["runs"], quotes, gross_cash_fraction=base_fraction,
                        latency_seconds=float(latency), depth_haircut=float(haircut),
                        exit_cache=baseline_cache,
                    )
                    rows.append({
                        "slippage": float(slippage), "latency_seconds": float(latency),
                        "depth_haircut": float(haircut), "window_index": int(window["window_index"]),
                        "start_run": window["start_run"], "end_run": window["end_run"],
                        "adaptive": {key: value for key, value in adaptive.items() if key != "chain"},
                        "baseline": {key: value for key, value in baseline.items() if key != "chain"},
                        "ending_cash_delta": float(adaptive["first_ten_ending_cash"] - baseline["first_ten_ending_cash"]),
                        "min_cash_delta": float(adaptive["first_ten_min_cash_before_entry"] - baseline["first_ten_min_cash_before_entry"]),
                    })
    frame = pd.DataFrame(rows)
    adaptive_series = frame["adaptive"].apply(pd.Series)
    baseline_series = frame["baseline"].apply(pd.Series)
    ending_delta = frame["ending_cash_delta"]
    min_delta = frame["min_cash_delta"]
    result: dict[str, Any] = {
        "format": result_format, "hypothesis_id": hypothesis_id,
        "source_commit": source_commit, "observed_source_commit": observed,
        "strict_quality": True, "complete_runs": len(runs),
        "feature_rows": int(len(features)), "window_count": len(windows),
        "policy": {
            "base_fraction": float(base_fraction), "boost_fraction": float(boost_fraction),
            "boost_after_profitable_exits": int(boost_after_profits),
            "online_observable": True, "no_future_exit_used_for_entry": True,
        },
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": MINIMUM_ORDER_USD, "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE, "same_ledger_within_window": True,
            "no_cross_window_concatenation": True, "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "stress_grid": {
            "slippages": list(SLIPPAGES), "latencies_seconds": list(LATENCIES_SECONDS),
            "depth_haircuts": list(DEPTH_HAIRCUTS), "rows": int(len(frame)),
        },
        "adaptive": {
            "all_ten": bool(adaptive_series["first_ten_passed"].all()),
            "pass_rate": float(adaptive_series["first_ten_passed"].mean()),
            "min_fills": int(adaptive_series["first_ten_fills"].min()),
            "min_cash": float(adaptive_series["first_ten_ending_cash"].min()),
            "median_cash": float(adaptive_series["first_ten_ending_cash"].median()),
            "mean_boost_entries": float(adaptive_series["boost_entries"].mean()),
        },
        "baseline": {
            "all_ten": bool(baseline_series["first_ten_passed"].all()),
            "pass_rate": float(baseline_series["first_ten_passed"].mean()),
            "min_cash": float(baseline_series["first_ten_ending_cash"].min()),
            "median_cash": float(baseline_series["first_ten_ending_cash"].median()),
        },
        "comparison": {
            "min_cash_delta": float(min_delta.min()),
            "median_ending_cash_delta": float(ending_delta.median()),
            "mean_ending_cash_delta": float(ending_delta.mean()),
            "cash_floor_non_degraded": bool(min_delta.min() >= -1e-9),
            "growth_non_degraded": bool(ending_delta.median() > 0.01),
        },
        "decision": f"{hypothesis_id.split('_', 1)[0].lower()}_adaptive_boost_passes" if (
            bool(adaptive_series["first_ten_passed"].all())
            and bool(min_delta.min() >= -1e-9)
            and bool(ending_delta.median() > 0.01)
        ) else f"{hypothesis_id.split('_', 1)[0].lower()}_adaptive_boost_rejected_keep_h534",
        "new_data_collected": False, "model_refit": True,
        "live_orders_enabled": False, "actual_fills": 0,
        "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "evidence_boundary": "old-clean paper replay; no authenticated execution or reusable cash",
        "root_cause": "Tests whether a small online boost after observed profitable exits improves growth without weakening H534's continuous ten-fill cash floor.",
        "rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_metrics.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_bytes = json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n"
    (output / "summary.json").write_bytes(summary_bytes)
    result["summary_sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("decision", "adaptive", "baseline", "comparison")}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hypothesis-id", default=HYPOTHESIS_ID)
    parser.add_argument("--result-format", default=RESULT_FORMAT)
    parser.add_argument("--base-fraction", type=float, default=BASE_FRACTION)
    parser.add_argument("--boost-fraction", type=float, default=BOOST_FRACTION)
    parser.add_argument("--boost-after-profits", type=int, default=BOOST_AFTER_PROFITS)
    args = parser.parse_args()
    evaluate(
        args.source_root,
        source_commit=args.source_commit,
        output=args.output,
        hypothesis_id=args.hypothesis_id,
        result_format=args.result_format,
        base_fraction=args.base_fraction,
        boost_fraction=args.boost_fraction,
        boost_after_profits=args.boost_after_profits,
    )


if __name__ == "__main__":
    main()
